"""
VPS Subtitle Cloud Dispatcher & Manager
---------------------------------------
Ultra-lightweight daemon running on the VPS (~20MB RAM, 0% CPU).
Replaces heavy 'reporter.py' and 'auto_sub_hunter.py'.

Concurrency Architecture:
- Dedicated 5 slots for Reporter jobs (report_status == 'pending')
- Dedicated 5 slots for Sub Hunter jobs (subtitles.sinhala == 'not_found')
- Total concurrent GitHub Actions workflows: 10 (5 + 5)
- Neither bot starves the other!
"""

import os
import sys
import time
import uuid
import threading
import requests
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter

# 🛡️ gRPC HTTP/2 Keepalive configuration to prevent ENHANCE_YOUR_CALM (too_many_pings)
os.environ["GRPC_ARG_KEEPALIVE_TIME_MS"] = "300000"
os.environ["GRPC_ARG_KEEPALIVE_TIMEOUT_MS"] = "20000"
os.environ["GRPC_ARG_HTTP2_MIN_SENT_PING_INTERVAL_WITHOUT_DATA_MS"] = "300000"
os.environ["GRPC_ARG_HTTP2_MAX_PINGS_WITHOUT_DATA"] = "0"
os.environ["GRPC_ARG_KEEPALIVE_PERMIT_WITHOUT_CALLS"] = "0"

import google.cloud.firestore_v1.base_client as bclient
bclient._DEFAULT_CHANNEL_OPTIONS = [
    ('grpc.keepalive_time_ms', 300000),
    ('grpc.keepalive_timeout_ms', 20000),
    ('grpc.http2.min_sent_ping_interval_without_data_ms', 300000),
    ('grpc.http2.max_pings_without_data', 0),
    ('grpc.keepalive_permit_without_calls', 0),
    ('grpc.max_send_message_length', -1),
    ('grpc.max_receive_message_length', -1),
]

load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "uploader", ".env"))

WORKER_ID = f"SubManager-{uuid.uuid4().hex[:4]}"
MAX_CONCURRENT_REPORTS = 5
MAX_CONCURRENT_HUNTS = 5
JOB_TIMEOUT_SECONDS = 600  # 10 minutes timeout per cloud workflow

# GitHub Dispatch Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_WORKFLOW_REPO") or os.getenv("GITHUB_REPO", "Anishift-svr/sub-vault-160633")

# Initialize Firebase with fallback and retry
key_candidates = [
    os.path.join(os.path.dirname(__file__), "serviceAccountKey.json"),
    "serviceAccountKey.json",
    os.path.join(os.path.dirname(__file__), "..", "serviceAccountKey.json"),
    os.path.join(os.path.dirname(__file__), "..", "uploader", "serviceAccountKey.json")
]
key_path = next((p for p in key_candidates if os.path.exists(p)), "serviceAccountKey.json")

for attempt in range(5):
    try:
        if not firebase_admin._apps:
            cred = credentials.Certificate(key_path)
            firebase_admin.initialize_app(cred, {
                'databaseURL': os.getenv('FIREBASE_DB_URL', 'https://anishift-5d14b-default-rtdb.firebaseio.com/')
            })
        db = firestore.client()
        break
    except Exception as e:
        print(f"[{WORKER_ID}] ⚠️ Firebase init attempt {attempt+1} failed: {e}. Retrying...", flush=True)
        time.sleep(3)
else:
    raise RuntimeError("Failed to connect to Firebase.")

# Independent Concurrency & Queue state
active_reports = {}  # ep_path -> { "dispatched_at": float, "doc_ref": DocumentReference }
active_hunts = {}    # ep_path -> { "dispatched_at": float, "doc_ref": DocumentReference }

report_queue = []    # list of doc
hunt_queue = []      # list of doc

queue_lock = threading.Lock()
seen_report_paths = set()
seen_hunt_paths = set()

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] [{WORKER_ID}] {msg}", flush=True)

def dispatch_to_github(job_type, ep_path, anime_id, ep_num, doc_id):
    """Triggers GitHub Actions workflow via repository_dispatch"""
    if not GITHUB_TOKEN:
        log("❌ GITHUB_TOKEN is missing in .env! Cannot dispatch workflow.")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/dispatches"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "User-Agent": "Anishift-SubManager-Dispatcher"
    }

    payload = {
        "event_type": "process_subtitle_job",
        "client_payload": {
            "job_type": job_type,
            "episode_path": ep_path,
            "anime_id": str(anime_id),
            "ep_num": int(ep_num),
            "doc_id": doc_id,
            "dispatched_at": time.time()
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 204:
            log(f"🚀 Dispatched {job_type.upper()} Cloud Workflow: {ep_path} to {GITHUB_REPO}")
            return True
        else:
            log(f"❌ GitHub Dispatch failed [HTTP {resp.status_code}]: {resp.text} (Repo: {GITHUB_REPO})")
            return False
    except Exception as e:
        log(f"❌ GitHub Dispatch exception: {e}")
        return False

def monitor_active_dispatches():
    """Background thread that monitors running cloud workflows and frees slots"""
    while True:
        try:
            time.sleep(10)
            now = time.time()

            with queue_lock:
                # 1. Monitor Reports
                to_remove_reports = []
                for ep_path, info in list(active_reports.items()):
                    elapsed = now - info["dispatched_at"]
                    if elapsed > JOB_TIMEOUT_SECONDS:
                        log(f"⏰ Report workflow for {ep_path} timed out after {int(elapsed)}s. Freeing slot.")
                        to_remove_reports.append(ep_path)
                        continue

                    try:
                        snap = info["doc_ref"].get()
                        if snap.exists:
                            data = snap.to_dict() or {}
                            st = data.get("report_status")
                            if st in ["fixed", "failed_no_video", "failed_no_sub_in_video", "failed_translation_or_empty", "error"]:
                                log(f"✅ Cloud Report Finished for {ep_path} (Status: {st})")
                                to_remove_reports.append(ep_path)
                    except Exception:
                        pass

                for path in to_remove_reports:
                    active_reports.pop(path, None)
                    seen_report_paths.discard(path)

                # 2. Monitor Hunts
                to_remove_hunts = []
                for ep_path, info in list(active_hunts.items()):
                    elapsed = now - info["dispatched_at"]
                    if elapsed > JOB_TIMEOUT_SECONDS:
                        log(f"⏰ Hunt workflow for {ep_path} timed out after {int(elapsed)}s. Freeing slot.")
                        to_remove_hunts.append(ep_path)
                        continue

                    try:
                        snap = info["doc_ref"].get()
                        if snap.exists:
                            data = snap.to_dict() or {}
                            si_sub = data.get("subtitles", {}).get("sinhala")
                            if si_sub and si_sub not in ["not_found", "processing_cloud", "processing_dual"]:
                                log(f"✅ Cloud Hunt Finished for {ep_path} (Sub: {si_sub[:30]}...)")
                                to_remove_hunts.append(ep_path)
                            elif si_sub == "not_found" and elapsed > 60:
                                log(f"ℹ️ Cloud Hunt completed (No sub available) for {ep_path}")
                                to_remove_hunts.append(ep_path)
                    except Exception:
                        pass

                for path in to_remove_hunts:
                    active_hunts.pop(path, None)
                    seen_hunt_paths.discard(path)

        except Exception as e:
            log(f"⚠️ Monitor error: {e}")

def dispatcher_loop():
    """Takes jobs from queues and dispatches up to 5 Reports and 5 Hunts (10 total)"""
    while True:
        # Check Report Queue
        report_job = None
        with queue_lock:
            if len(active_reports) < MAX_CONCURRENT_REPORTS and report_queue:
                report_job = report_queue.pop(0)

        if report_job:
            ep_ref = report_job.reference
            ep_path = ep_ref.path
            data = report_job.to_dict() or {}
            series_ref = ep_ref.parent.parent if ep_ref.parent else None
            anime_id = series_ref.id if series_ref else data.get('anilist_id', 0)
            ep_num = data.get('episodeNumber', 1)

            success = dispatch_to_github("report", ep_path, anime_id, ep_num, report_job.id)
            with queue_lock:
                if success:
                    active_reports[ep_path] = {
                        "dispatched_at": time.time(),
                        "doc_ref": ep_ref
                    }
                    log(f"📊 Active Reports: {len(active_reports)}/{MAX_CONCURRENT_REPORTS} | Active Hunts: {len(active_hunts)}/{MAX_CONCURRENT_HUNTS}")
                else:
                    seen_report_paths.discard(ep_path)

        # Check Hunt Queue
        hunt_job = None
        with queue_lock:
            if len(active_hunts) < MAX_CONCURRENT_HUNTS and hunt_queue:
                hunt_job = hunt_queue.pop(0)

        if hunt_job:
            ep_ref = hunt_job.reference
            ep_path = ep_ref.path
            data = hunt_job.to_dict() or {}
            series_ref = ep_ref.parent.parent if ep_ref.parent else None
            anime_id = series_ref.id if series_ref else data.get('anilist_id', 0)
            ep_num = data.get('episodeNumber', 1)

            success = dispatch_to_github("hunt", ep_path, anime_id, ep_num, hunt_job.id)
            with queue_lock:
                if success:
                    active_hunts[ep_path] = {
                        "dispatched_at": time.time(),
                        "doc_ref": ep_ref
                    }
                    log(f"📊 Active Reports: {len(active_reports)}/{MAX_CONCURRENT_REPORTS} | Active Hunts: {len(active_hunts)}/{MAX_CONCURRENT_HUNTS}")
                else:
                    seen_hunt_paths.discard(ep_path)

        if not report_job and not hunt_job:
            time.sleep(3)
        else:
            time.sleep(1)

def queue_report_job(doc):
    ep_path = doc.reference.path
    with queue_lock:
        if ep_path in seen_report_paths or ep_path in active_reports:
            return
        seen_report_paths.add(ep_path)
        report_queue.append(doc)
        log(f"📥 Queued REPORT job: {ep_path} (Report Queue Size: {len(report_queue)})")

def queue_hunt_job(doc):
    ep_path = doc.reference.path
    with queue_lock:
        if ep_path in seen_hunt_paths or ep_path in active_hunts:
            return
        seen_hunt_paths.add(ep_path)
        hunt_queue.append(doc)
        log(f"📥 Queued HUNT job: {ep_path} (Hunt Queue Size: {len(hunt_queue)})")

# Realtime Listeners
def on_reports_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name in ['ADDED', 'MODIFIED']:
            doc = change.document
            data = doc.to_dict() or {}
            if data.get('report_status') == 'pending' and data.get('report_reason') == 'missing_subs':
                queue_report_job(doc)

def on_hunts_snapshot(col_snapshot, changes, read_time):
    for change in changes:
        if change.type.name in ['ADDED', 'MODIFIED']:
            doc = change.document
            data = doc.to_dict() or {}
            if data.get('subtitles', {}).get('sinhala') == 'not_found':
                queue_hunt_job(doc)

def periodic_sweeper():
    """Periodic safety sweep every 5 minutes to guarantee no missed Firestore jobs"""
    while True:
        try:
            time.sleep(300)
            # 1. Sweep reports
            r_docs = db.collection_group('episodes').where(filter=FieldFilter('report_status', '==', 'pending')).limit(50).get()
            for d in r_docs:
                if (d.to_dict() or {}).get('report_reason') == 'missing_subs':
                    queue_report_job(d)

            # 2. Sweep missing subs
            h_docs = db.collection_group('episodes').where(filter=FieldFilter('subtitles.sinhala', '==', 'not_found')).limit(50).get()
            for d in h_docs:
                queue_hunt_job(d)
        except Exception as e:
            log(f"⚠️ Sweeper error: {e}")

def main():
    log("=======================================================")
    log("🚀 VPS Sub Manager Started (Dual Independent Pools)")
    log(f"📦 Target Workflow Repo: {GITHUB_REPO}")
    log(f"⚡ Max Concurrent Reports: {MAX_CONCURRENT_REPORTS}")
    log(f"⚡ Max Concurrent Hunts:   {MAX_CONCURRENT_HUNTS}")
    log(f"🔥 Total Max Workflows:    {MAX_CONCURRENT_REPORTS + MAX_CONCURRENT_HUNTS}")
    log("=======================================================")

    # Initial startup sweep
    try:
        r_docs = db.collection_group('episodes').where(filter=FieldFilter('report_status', '==', 'pending')).limit(50).get()
        for d in r_docs:
            if (d.to_dict() or {}).get('report_reason') == 'missing_subs':
                queue_report_job(d)

        h_docs = db.collection_group('episodes').where(filter=FieldFilter('subtitles.sinhala', '==', 'not_found')).limit(50).get()
        for d in h_docs:
            queue_hunt_job(d)
    except Exception as e:
        log(f"⚠️ Initial sweep warning: {e}")

    threading.Thread(target=dispatcher_loop, daemon=True).start()
    threading.Thread(target=monitor_active_dispatches, daemon=True).start()
    threading.Thread(target=periodic_sweeper, daemon=True).start()

    # Firestore realtime listeners
    query_reports = db.collection_group('episodes').where(filter=FieldFilter('report_status', '==', 'pending'))
    query_reports.on_snapshot(on_reports_snapshot)

    query_hunts = db.collection_group('episodes').where(filter=FieldFilter('subtitles.sinhala', '==', 'not_found'))
    query_hunts.on_snapshot(on_hunts_snapshot)

    log("👂 Real-time Listeners Active! VPS CPU & RAM are 100% protected.")

    while True:
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()
