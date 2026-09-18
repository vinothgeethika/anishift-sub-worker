"""
GitHub Cloud Subtitle Worker
----------------------------
Runs inside GitHub Actions (2 vCPU, 7 GB RAM).
Executes either 'report' jobs (reporter.py logic) or 'hunt' jobs (auto_sub_hunter.py logic).
- Fetches candidate subtitles from RPMShare
- Cleans and formats dialogs
- Translates to Sinhala using high-performance Universal Sub Engine
- Uploads English.srt and Sinhala.srt to GitHub Releases (DDL)
- Attaches Sinhala sub to RPMShare video
- Updates Firestore and clears RTDB Missing Subtitle Alerts
"""

import os
import sys
import json
import time
import uuid
import re
import shutil
import requests
import pysubs2
import firebase_admin
from firebase_admin import credentials, firestore, db as rtdb
from dotenv import load_dotenv

# Ensure sub_engine can be imported
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    from sub_engine import (
        process_sinhala_sub,
        process_english_sub,
        upload_to_github_release,
        upload_sub_to_rpm,
        delete_existing_sinhala_subs,
        clear_missing_sub_alert,
        push_missing_sub_alert,
        detect_encoding,
        is_valid_sub_file,
        MIN_SUB_LINE_THRESHOLD
    )
except ImportError:
    from uploader.sub_engine import (
        process_sinhala_sub,
        process_english_sub,
        upload_to_github_release,
        upload_sub_to_rpm,
        delete_existing_sinhala_subs,
        clear_missing_sub_alert,
        push_missing_sub_alert,
        detect_encoding,
        is_valid_sub_file,
        MIN_SUB_LINE_THRESHOLD
    )

load_dotenv()

WORKER_ID = f"CloudSub-{uuid.uuid4().hex[:6]}"
RPM_BASE_URL = "https://rpmshare.com/api/v1"
RPM_API_TOKEN = os.getenv("RPMSHARE_API_TOKEN", "")
RPM_API_TOKEN_2 = os.getenv("RPMSHARE_API_TOKEN_2", "")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://anishift-5d14b-default-rtdb.firebaseio.com/")

def init_firebase():
    key_path = "serviceAccountKey.json" if os.path.exists("serviceAccountKey.json") else os.path.join("..", "serviceAccountKey.json")
    if not os.path.exists(key_path):
        firebase_json = os.getenv("FIREBASE_JSON")
        if firebase_json:
            with open("serviceAccountKey.json", "w", encoding="utf-8") as f:
                f.write(firebase_json)
            key_path = "serviceAccountKey.json"

    if not firebase_admin._apps:
        cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred, {'databaseURL': FIREBASE_DB_URL})
    return firestore.client()

def download_rpm_sub_api(video_id, api_token=RPM_API_TOKEN, work_dir="."):
    print(f"[{WORKER_ID}] 📥 Extracting Sub from RPM Video: {video_id}", flush=True)
    headers = {'api-token': api_token}
    target_host = "https://rpmshare.com"

    try:
        player_resp = requests.get(f"{RPM_BASE_URL}/video/player/default", headers=headers, timeout=10)
        if player_resp.status_code == 200:
            p_dom = player_resp.json().get('domain')
            if p_dom:
                target_host = f"https://{p_dom}" if not p_dom.startswith("http") else p_dom
    except Exception:
        pass

    for _ in range(5):
        try:
            files_resp = requests.get(f"{RPM_BASE_URL}/video/manage/{video_id}/files", headers=headers, timeout=20)
            if files_resp.status_code == 200:
                files = files_resp.json()
                candidates = [f for f in files if f.get('type') == 'Subtitle' and f.get('language') != 'si']

                if candidates:
                    en_candidates = [c for c in candidates if c.get('language') == 'en' or 'eng' in c.get('name', '').lower()]
                    other_candidates = [c for c in candidates if c not in en_candidates]
                    sorted_candidates = en_candidates + other_candidates

                    valid_subs_data = []
                    print(f"[{WORKER_ID}] 🔍 Analyzing {len(sorted_candidates)} subtitle tracks...", flush=True)

                    for c in sorted_candidates:
                        try:
                            dl_resp = requests.get(f"{target_host}{c['url']}", timeout=30)
                            if dl_resp.status_code == 200:
                                ext = c.get('extension', 'srt')
                                temp_path = os.path.join(work_dir, f"temp_check_{uuid.uuid4().hex[:6]}.{ext}")
                                with open(temp_path, "wb") as f:
                                    f.write(dl_resp.content)

                                if not is_valid_sub_file(temp_path):
                                    if os.path.exists(temp_path): os.remove(temp_path)
                                    continue

                                enc = detect_encoding(temp_path)
                                try: subs = pysubs2.load(temp_path, encoding=enc)
                                except Exception: subs = pysubs2.load(temp_path, encoding='latin-1')

                                line_count = len(subs.events)
                                sub_lang = c.get('language', '').lower()

                                if line_count >= MIN_SUB_LINE_THRESHOLD:
                                    valid_subs_data.append({
                                        'path': temp_path,
                                        'lines': line_count,
                                        'lang': sub_lang,
                                        'is_en': (sub_lang == 'en' or 'eng' in c.get('name', '').lower())
                                    })
                                else:
                                    if os.path.exists(temp_path): os.remove(temp_path)
                        except Exception:
                            pass

                    if valid_subs_data:
                        valid_subs_data.sort(key=lambda x: (1 if x['is_en'] else 0, x['lines']), reverse=True)
                        winner = valid_subs_data[0]
                        print(f"[{WORKER_ID}] 🏆 WINNER: '{winner['lang']}' with {winner['lines']} lines!", flush=True)

                        for item in valid_subs_data[1:]:
                            if os.path.exists(item['path']):
                                try: os.remove(item['path'])
                                except Exception: pass
                        return winner['path']
        except Exception:
            pass
        time.sleep(2)
    return None

def fetch_dual_subs_from_rpm(video_id, api_token, work_dir="."):
    headers = {'api-token': api_token}
    target_host = "https://rpmshare.com"

    try:
        player_resp = requests.get(f"{RPM_BASE_URL}/video/player/default", headers=headers, timeout=10)
        if player_resp.status_code == 200:
            p_dom = player_resp.json().get('domain')
            if p_dom:
                target_host = f"https://{p_dom}" if not p_dom.startswith("http") else p_dom
    except Exception:
        pass

    try:
        files_resp = requests.get(f"{RPM_BASE_URL}/video/manage/{video_id}/files", headers=headers, timeout=15)
        if files_resp.status_code != 200:
            return None, None

        files = files_resp.json()
        sub_files = [f for f in files if f.get('type') == 'Subtitle']
        if not sub_files:
            return None, None

        print(f"[{WORKER_ID}] 🔍 Analyzing {len(sub_files)} subtitle tracks from RPM...", flush=True)
        si_candidates = []
        other_candidates = []

        for sf in sub_files:
            url = f"{target_host}{sf.get('url')}"
            name = sf.get('name', 'Unnamed')
            name_lower = name.lower()
            lang = sf.get('language', '').lower()
            ext = sf.get('extension', 'srt')
            tmp = os.path.join(work_dir, f"dual_sub_{uuid.uuid4().hex[:6]}.{ext}")

            try:
                dl = requests.get(url, timeout=20)
                if dl.status_code != 200:
                    continue
                with open(tmp, 'wb') as f:
                    f.write(dl.content)
            except Exception:
                continue

            if not is_valid_sub_file(tmp):
                if os.path.exists(tmp): os.remove(tmp)
                continue

            try:
                enc = detect_encoding(tmp)
                try: sub_obj = pysubs2.load(tmp, encoding=enc)
                except Exception: sub_obj = pysubs2.load(tmp, encoding='latin-1')

                lines = len(sub_obj.events)

                # --- 🟢 125-Line Threshold & Subtitle Scoring Logic ---
                score = lines
                if lines >= MIN_SUB_LINE_THRESHOLD:
                    if any(x in name_lower for x in ['si', 'sinhala', 'සිංහල']) or lang == 'si':
                        score += 200000
                    elif any(x in name_lower for x in ['en', 'eng', 'english']) or lang == 'en':
                        score += 100000
                    elif any(x in name_lower for x in ['ja', 'jap', 'romaji']) or lang == 'ja':
                        score -= 100000
                    else:
                        score += 10000  # Valid other language (e.g. French, Spanish, Track 1)
                else:
                    score -= 50000  # Penalize cracked/incomplete tracks

                print(f"[{WORKER_ID}]    📄 Track '{name}' | Lang: {lang or 'N/A'} | Lines: {lines} | Score: {score}", flush=True)

                if score <= 0:
                    if os.path.exists(tmp): os.remove(tmp)
                    continue

                track_info = {
                    'path': tmp,
                    'lines': lines,
                    'score': score,
                    'name': name,
                    'lang': lang
                }

                if any(x in name_lower for x in ['si', 'sinhala', 'සිංහල']) or lang == 'si':
                    si_candidates.append(track_info)
                else:
                    other_candidates.append(track_info)

            except Exception:
                if os.path.exists(tmp):
                    try: os.remove(tmp)
                    except Exception: pass

        winner_si_path = None
        if si_candidates:
            si_candidates.sort(key=lambda x: x['score'], reverse=True)
            winner_si = si_candidates[0]
            winner_si_path = winner_si['path']
            print(f"[{WORKER_ID}] 🏆 WINNER SINHALA: '{winner_si['name']}' ({winner_si['lines']} lines, Score: {winner_si['score']})", flush=True)
            for c in si_candidates[1:]:
                if os.path.exists(c['path']):
                    try: os.remove(c['path'])
                    except Exception: pass

        winner_cand_path = None
        if other_candidates:
            other_candidates.sort(key=lambda x: x['score'], reverse=True)
            winner_cand = other_candidates[0]
            winner_cand_path = winner_cand['path']
            print(f"[{WORKER_ID}] 🏆 WINNER TRANSLATION SOURCE: '{winner_cand['name']}' ({winner_cand['lines']} lines, Score: {winner_cand['score']})", flush=True)
            for c in other_candidates[1:]:
                if os.path.exists(c['path']):
                    try: os.remove(c['path'])
                    except Exception: pass

        return winner_si_path, winner_cand_path
    except Exception as e:
        print(f"[{WORKER_ID}] ❌ Error fetching dual RPM subs: {e}", flush=True)
        return None, None

def execute_report_job(db, payload, work_dir):
    ep_path = payload.get("episode_path")
    ep_ref = db.document(ep_path)
    snap = ep_ref.get()
    if not snap.exists:
        print(f"[{WORKER_ID}] ❌ Episode not found: {ep_path}", flush=True)
        return False

    data = snap.to_dict() or {}
    rpm_id = data.get('links', {}).get('rpm_video_id')
    if not rpm_id:
        stream_url = data.get('links', {}).get('rpm_stream') or data.get('links', {}).get('rpm_download') or ""
        m = re.search(r'/(?:v|d|embed)/([a-zA-Z0-9_-]+)', stream_url)
        if m: rpm_id = m.group(1)
        else:
            m2 = re.search(r'rpmshare\.com/v([a-zA-Z0-9_-]+)', stream_url)
            if m2: rpm_id = m2.group(1)

    if not rpm_id:
        print(f"[{WORKER_ID}] ❌ No RPM Video ID for {ep_path}", flush=True)
        ep_ref.update({'report_status': 'failed_no_video'})
        return False

    server = data.get('server', 1)
    token = RPM_API_TOKEN_2 if server == 2 else RPM_API_TOKEN
    series_ref = ep_ref.parent.parent if ep_ref.parent else None
    anime_id = series_ref.id if series_ref else data.get('anilist_id', 0)
    ep_num = data.get('episodeNumber', 1)

    job_id = payload.get("job_id") or f"report_{payload.get('doc_id')}"
    sub_file = download_rpm_sub_api(rpm_id, api_token=token, work_dir=work_dir)
    if not sub_file:
        print(f"[{WORKER_ID}] ⚠️ No valid subtitle file in video {rpm_id}", flush=True)
        ep_ref.update({'report_status': 'failed_no_sub_in_video', 'worker_id': None})
        try:
            rtdb.reference(f"sub_manager_jobs/completed_jobs/{job_id}").set({
                "status": "completed",
                "result": "failed_no_sub_in_video",
                "ep_path": ep_path,
                "timestamp": int(time.time() * 1000)
            })
        except Exception: pass
        return False

    rel_ctx = {}
    out_en = os.path.join(work_dir, f"english_sub_{uuid.uuid4().hex[:4]}.srt")
    out_si = os.path.join(work_dir, f"sinhala_sub_{uuid.uuid4().hex[:4]}.srt")

    eng_sub = process_english_sub(sub_file, out_name=out_en, log_prefix=f"[{WORKER_ID}]")
    github_en = upload_to_github_release(eng_sub, asset_name="English.srt", release_context=rel_ctx) if eng_sub else None

    sin_sub = process_sinhala_sub(sub_file, out_name=out_si, max_workers=5, log_prefix=f"[{WORKER_ID}]")
    github_si = None

    if sin_sub:
        github_si = upload_to_github_release(sin_sub, asset_name="Sinhala.srt", release_context=rel_ctx)
        delete_existing_sinhala_subs(rpm_id, api_token=token)
        upload_sub_to_rpm(rpm_id, sin_sub, api_token=token, remote_url=github_si)

    if github_si:
        ep_ref.update({
            'status': 'uploaded',
            'report_status': 'fixed',
            'subtitles.sinhala': github_si,
            'subtitles.english': github_en if github_en else 'not_found',
            'last_fixed': firestore.SERVER_TIMESTAMP,
            'worker_id': None
        })
        clear_missing_sub_alert(rtdb, anime_id, ep_num)
        print(f"[{WORKER_ID}] ✅ SUCCESS! Report Fixed. SI: {github_si} | EN: {github_en}", flush=True)
        try:
            rtdb.reference(f"sub_manager_jobs/completed_jobs/{job_id}").set({
                "status": "completed",
                "result": "fixed",
                "ep_path": ep_path,
                "timestamp": int(time.time() * 1000)
            })
        except Exception: pass
        return True
    else:
        ep_ref.update({'report_status': 'failed_translation_or_empty', 'worker_id': None})
        try:
            rtdb.reference(f"sub_manager_jobs/completed_jobs/{job_id}").set({
                "status": "completed",
                "result": "failed_translation",
                "ep_path": ep_path,
                "timestamp": int(time.time() * 1000)
            })
        except Exception: pass
        return False

def execute_hunt_job(db, payload, work_dir):
    ep_path = payload.get("episode_path")
    ep_ref = db.document(ep_path)
    snap = ep_ref.get()
    if not snap.exists:
        print(f"[{WORKER_ID}] ❌ Episode not found: {ep_path}", flush=True)
        return False

    data = snap.to_dict() or {}
    rpm_id = data.get('links', {}).get('rpm_video_id')
    server = data.get('server', 1)
    token = RPM_API_TOKEN_2 if server == 2 else RPM_API_TOKEN

    if not rpm_id:
        stream_url = data.get('links', {}).get('rpm_stream') or data.get('links', {}).get('rpm_download') or ""
        m = re.search(r'/(?:v|d|embed)/([a-zA-Z0-9_-]+)', stream_url)
        if m: rpm_id = m.group(1)
        else:
            m2 = re.search(r'rpmshare\.com/v([a-zA-Z0-9_-]+)', stream_url)
            if m2: rpm_id = m2.group(1)

    if not rpm_id:
        ep_ref.update({'subtitles.sinhala': 'failed_no_rpm_id'})
        return False

    series_ref = ep_ref.parent.parent if ep_ref.parent else None
    anime_id = series_ref.id if series_ref else data.get('anilist_id', 0)
    ep_num = data.get('episodeNumber', 1)
    ep_title = data.get('episodeTitle') or f"Episode {ep_num}"

    ep_ref.update({'subtitles.sinhala': 'processing_cloud'})
    print(f"[{WORKER_ID}] 🚀 Starting Hunt: {ep_title} (Ep {ep_num})", flush=True)

    si_path, en_path = fetch_dual_subs_from_rpm(rpm_id, token, work_dir=work_dir)
    final_si_url = None
    final_en_url = None
    rel_ctx = {}

    if en_path:
        print(f"[{WORKER_ID}] 🔵 EN Sub Found. Uploading English.srt...", flush=True)
        en_processed = process_english_sub(en_path, log_prefix=f"[{WORKER_ID}]")
        target_en = en_processed if en_processed else en_path
        final_en_url = upload_to_github_release(target_en, asset_name="English.srt", release_context=rel_ctx)

        if not si_path:
            print(f"[{WORKER_ID}] 🔄 Translating English sub to Sinhala...", flush=True)
            si_gen_path = process_sinhala_sub(
                target_en,
                out_name=os.path.join(work_dir, f"sinhala_{uuid.uuid4().hex[:4]}.srt"),
                max_workers=5,
                log_prefix=f"[{WORKER_ID}]"
            )
            if si_gen_path:
                final_si_url = upload_to_github_release(si_gen_path, asset_name="Sinhala.srt", release_context=rel_ctx)
                delete_existing_sinhala_subs(rpm_id, token)
                upload_sub_to_rpm(rpm_id, si_gen_path, token, remote_url=final_si_url)

    if si_path:
        print(f"[{WORKER_ID}] ⭐ Direct Sinhala Sub Found on RPM!", flush=True)
        si_clean_path = process_sinhala_sub(
            si_path,
            out_name=os.path.join(work_dir, f"sinhala_{uuid.uuid4().hex[:4]}.srt"),
            max_workers=5,
            log_prefix=f"[{WORKER_ID}]"
        )
        target_si = si_clean_path if si_clean_path else si_path
        final_si_url = upload_to_github_release(target_si, asset_name="Sinhala.srt", release_context=rel_ctx)
        delete_existing_sinhala_subs(rpm_id, token)
        upload_sub_to_rpm(rpm_id, target_si, token, remote_url=final_si_url)

    job_id = payload.get("job_id") or f"hunt_{payload.get('doc_id')}"
    updates = {'last_auto_update': firestore.SERVER_TIMESTAMP}
    if final_si_url:
        updates['subtitles.sinhala'] = final_si_url
        updates['status'] = 'uploaded'
        clear_missing_sub_alert(rtdb, anime_id, ep_num)
        print(f"[{WORKER_ID}] ✅ Hunt SUCCESS! Sinhala Sub Online: {final_si_url}", flush=True)
    else:
        # Mark as 'no_sub_available' so Firestore listeners NEVER loop infinitely on this document!
        updates['subtitles.sinhala'] = 'no_sub_available'
        updates['hunt_status'] = 'no_sub_available'
        push_missing_sub_alert(rtdb, anime_id, ep_title, ep_num, rpm_id, server)
        print(f"[{WORKER_ID}] ⚠️ Hunt COMPLETED: No subtitle available on RPM. Marked 'no_sub_available'.", flush=True)

    if final_en_url:
        updates['subtitles.english'] = final_en_url

    ep_ref.update(updates)
    try:
        rtdb.reference(f"sub_manager_jobs/completed_jobs/{job_id}").set({
            "status": "completed",
            "result": "success" if final_si_url else "no_sub_available",
            "ep_path": ep_path,
            "timestamp": int(time.time() * 1000)
        })
    except Exception: pass
    return bool(final_si_url)

def main():
    print(f"[{WORKER_ID}] 🤖 GitHub Cloud Sub Worker Starting...", flush=True)
    payload_str = os.getenv("JOB_PAYLOAD", "")
    if not payload_str:
        if len(sys.argv) > 1:
            payload_str = sys.argv[1]

    if not payload_str:
        print(f"[{WORKER_ID}] ❌ Error: No JOB_PAYLOAD provided. Exiting.", flush=True)
        sys.exit(1)

    try:
        payload = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
    except Exception as e:
        print(f"[{WORKER_ID}] ❌ Error parsing JOB_PAYLOAD JSON: {e}", flush=True)
        sys.exit(1)

    job_type = payload.get("job_type", "report")
    ep_path = payload.get("episode_path")
    print(f"[{WORKER_ID}] 📋 Job Type: {job_type.upper()} | Target: {ep_path}", flush=True)

    work_dir = f"temp_worker_{uuid.uuid4().hex[:6]}"
    os.makedirs(work_dir, exist_ok=True)

    try:
        db = init_firebase()
        if job_type == "hunt":
            success = execute_hunt_job(db, payload, work_dir)
        else:
            success = execute_report_job(db, payload, work_dir)

        print(f"[{WORKER_ID}] 🏁 Job Finished with Result: {'SUCCESS' if success else 'COMPLETED (NO SUB AVAILABLE)'}", flush=True)
        sys.exit(0)
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

if __name__ == "__main__":
    main()
