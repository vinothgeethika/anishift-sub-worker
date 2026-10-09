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
import base64
import time
import uuid
import re
import shutil
import io
import zipfile
import urllib.parse
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
RPM_API_TOKEN = os.getenv("RPMSHARE_API_TOKEN", "").strip().strip("'\"")
RPM_API_TOKEN_2 = os.getenv("RPMSHARE_API_TOKEN_2", "").strip().strip("'\"")
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://anishift-5d14b-default-rtdb.firebaseio.com/")
DEDICATED_RTDB_URL = os.getenv("DEDICATED_RTDB_URL", "https://anihsift-sever-2-default-rtdb.firebaseio.com").rstrip("/")

def get_rpm_tokens(preferred_token=None):
    tokens = []
    if preferred_token and str(preferred_token).strip():
        cp = str(preferred_token).strip().strip("'\"")
        if cp: tokens.append(cp)
    for t in [
        os.getenv("RPMSHARE_API_TOKEN_2", "").strip().strip("'\""),
        os.getenv("RPMSHARE_API_TOKEN", "").strip().strip("'\""),
        RPM_API_TOKEN_2,
        RPM_API_TOKEN
    ]:
        if t and t not in tokens:
            tokens.append(t)
    return tokens

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

def download_rpm_sub_api(video_id, api_token=None, work_dir=".", return_token=False):
    print(f"[{WORKER_ID}] 📥 Extracting Sub from RPM Video: {video_id}", flush=True)
    all_tokens = get_rpm_tokens(api_token)

    for token_idx, cur_token in enumerate(all_tokens, 1):
        tok_tag = f"...{cur_token[-6:]}" if len(cur_token) >= 6 else f"token_{token_idx}"
        headers = {'api-token': cur_token}
        target_host = "https://rpmshare.com"

        try:
            player_resp = requests.get(f"{RPM_BASE_URL}/video/player/default", headers=headers, timeout=10)
            if player_resp.status_code == 200:
                p_dom = player_resp.json().get('domain')
                if p_dom:
                    target_host = f"https://{p_dom}" if not p_dom.startswith("http") else p_dom
        except Exception:
            pass

        for attempt in range(3):
            try:
                files_resp = requests.get(f"{RPM_BASE_URL}/video/manage/{video_id}/files", headers=headers, timeout=20)
                if files_resp.status_code != 200:
                    # Token does not own this video
                    break
                files = files_resp.json()
                if not isinstance(files, list) or len(files) == 0:
                    break

                candidates = [f for f in files if f.get('type') == 'Subtitle' and f.get('language') != 'si']
                if not candidates:
                    break

                en_candidates = [c for c in candidates if c.get('language') == 'en' or 'eng' in c.get('name', '').lower()]
                other_candidates = [c for c in candidates if c not in en_candidates]
                sorted_candidates = en_candidates + other_candidates

                valid_subs_data = []
                print(f"[{WORKER_ID}] 🔍 Analyzing {len(sorted_candidates)} subtitle tracks (Token {tok_tag})...", flush=True)

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
                    print(f"[{WORKER_ID}] 🏆 WINNER: '{winner['lang']}' with {winner['lines']} lines (via {tok_tag})!", flush=True)

                    for item in valid_subs_data[1:]:
                        if os.path.exists(item['path']):
                            try: os.remove(item['path'])
                            except Exception: pass

                    if return_token:
                        return winner['path'], cur_token
                    return winner['path']
            except Exception:
                pass
            time.sleep(1)

    if return_token:
        return None, None
    return None

def fetch_dual_subs_from_rpm(video_id, api_token=None, work_dir=".", return_token=False):
    all_tokens = get_rpm_tokens(api_token)

    for token_idx, cur_token in enumerate(all_tokens, 1):
        tok_tag = f"...{cur_token[-6:]}" if len(cur_token) >= 6 else f"token_{token_idx}"
        headers = {'api-token': cur_token}
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
                continue

            files = files_resp.json()
            if not isinstance(files, list):
                continue

            sub_files = [f for f in files if f.get('type') == 'Subtitle']
            if not sub_files:
                continue

            print(f"[{WORKER_ID}] 🔍 Analyzing {len(sub_files)} subtitle tracks from RPM (Token {tok_tag})...", flush=True)
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
                            score += 10000
                    else:
                        score -= 50000

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

            if winner_si_path or winner_cand_path:
                if return_token:
                    return winner_si_path, winner_cand_path, cur_token
                return winner_si_path, winner_cand_path

        except Exception as e:
            print(f"[{WORKER_ID}] ❌ Error fetching dual RPM subs with {tok_tag}: {e}", flush=True)

    if return_token:
        return None, None, None
    return None, None

# =====================================================================
# ⚡ ABYSS AUTHENTICATION & SUBTITLE SYNC HELPERS
# =====================================================================
def find_owner_token_and_email(abyss_video_id, preferred_hint=None):
    """Dynamically finds which Abyss account owns this video ID and returns (jwt_token, email)"""
    try:
        r = requests.get(f"{DEDICATED_RTDB_URL}/abyss_accounts.json", timeout=10)
        if r.status_code != 200 or not r.json():
            return None, None
        accounts_map = r.json()
    except Exception as e:
        print(f"[{WORKER_ID}] ⚠️ Error fetching Abyss accounts: {e}", flush=True)
        return None, None

    acc_list = []
    for acc in accounts_map.values():
        if isinstance(acc, dict) and acc.get('password') and acc.get('email'):
            if preferred_hint and (acc.get('email') == preferred_hint or preferred_hint in acc.get('email', '')):
                acc_list.insert(0, acc)
            else:
                acc_list.append(acc)

    for acc in acc_list:
        email = acc.get('email')
        pwd = acc.get('password')
        try:
            res = requests.post("https://api.abyss.to/auth/login", json={"email": email, "password": pwd}, timeout=10)
            if res.status_code == 200 and res.json().get('token'):
                tok = res.json()['token']
                chk = requests.get(f"https://api.abyss.to/v1/subtitles/{abyss_video_id}/list", headers={'Authorization': f'Bearer {tok}'}, timeout=10)
                if chk.status_code == 200:
                    return tok, email
        except Exception:
            pass

    return None, None

def replace_abyss_sinhala_subtitle(abyss_video_id, sub_bytes, account_hint=None):
    """Deletes existing Sinhala subtitle on Abyss and pushes the new one using the owning account"""
    jwt_token, used_email = find_owner_token_and_email(abyss_video_id, account_hint)
    if not jwt_token:
        print(f"[{WORKER_ID}] ⚠️ Failed to locate owner Abyss account for video {abyss_video_id}", flush=True)
        return False, "Failed to locate owner Abyss account"

    # 1. Delete existing Sinhala sub if present
    try:
        list_res = requests.get(f"https://api.abyss.to/v1/subtitles/{abyss_video_id}/list", headers={'Authorization': f'Bearer {jwt_token}'}, timeout=15)
        if list_res.status_code == 200:
            items = list_res.json().get('items', [])
            for item in items:
                name = (item.get('name') or '').lower()
                lang = (item.get('language') or '').lower()
                label = (item.get('label') or '').lower()
                title = (item.get('title') or '').lower()
                
                is_sinhala = (
                    'sinhala' in name or 'sinhala' in lang or 'sinhala' in label or 'sinhala' in title or
                    lang in ['si', 'sin', 'sinhala'] or
                    'si' in name or 'si' in label
                )
                
                if is_sinhala:
                    sid = item.get('id')
                    del_res = requests.delete(f"https://api.abyss.to/v1/subtitles/{sid}", headers={'Authorization': f'Bearer {jwt_token}'}, timeout=15)
                    print(f"[{WORKER_ID}] 🗑️ Deleted old Abyss Sinhala sub ({sid}) on account {used_email} [Status: {del_res.status_code}]", flush=True)
                    time.sleep(0.5)
    except Exception as e:
        print(f"[{WORKER_ID}] ⚠️ Error checking/deleting old Abyss sub: {e}", flush=True)

    # 2. Upload new Sinhala subtitle (.srt)
    try:
        put_url = f"https://api.abyss.to/v1/upload/subtitles/{abyss_video_id}?language=Sinhala&filename=sinhala.srt"
        headers = {
            'Authorization': f'Bearer {jwt_token}',
            'Content-Type': 'application/octet-stream'
        }
        put_res = requests.put(put_url, headers=headers, data=sub_bytes, timeout=40)
        if put_res.status_code in [200, 201]:
            print(f"[{WORKER_ID}] 🎬 ✅ Uploaded Sinhala Sub to Abyss Video {abyss_video_id} ({used_email})", flush=True)
            return True, used_email
        else:
            return False, f"HTTP {put_res.status_code}: {put_res.text[:80]}"
    except Exception as e:
        return False, str(e)

def download_sinhala_sub_from_abyss(abyss_video_id):
    """Downloads Sinhala subtitle file (.srt) from Abyss video player CDN"""
    if not abyss_video_id:
        return None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://abyss.to/"
    }
    for attempt in range(3):
        try:
            player_url = f"https://player.abyssplayer.com/{abyss_video_id}"
            r = requests.get(player_url, headers=headers, timeout=15)
            if r.status_code == 200 and 'const datas =' in r.text:
                m = re.search(r'const\s+datas\s*=\s*"([^"]+)"', r.text)
                if m:
                    raw_bytes = base64.b64decode(m.group(1))
                    raw_text = raw_bytes.decode('latin-1', errors='replace')
                    raw_json = json.loads(raw_text)
                    md5_id = raw_json.get("md5_id")
                    subtitles = raw_json.get("config", {}).get("subtitles", [])
                    for sub in subtitles:
                        lang = (sub.get("lang") or "").lower()
                        slug = sub.get("slug")
                        sub_type = sub.get("type", "srt")
                        is_sinhala = any(k in lang for k in ['sinhala', 'si', 'sin']) or lang == 'si'
                        if is_sinhala and slug and md5_id:
                            cdn_url = f"https://cdn.iamcdn.net/subtitle/{md5_id}/{slug}.{sub_type}"
                            dl = requests.get(cdn_url, headers=headers, timeout=25)
                            if dl.status_code == 200 and len(dl.content) > 50:
                                print(f"[{WORKER_ID}] ✅ Extracted Sinhala sub from Abyss Player ({len(dl.content)} bytes)", flush=True)
                                return dl.content
            break
        except Exception as e:
            if attempt == 2:
                print(f"[{WORKER_ID}] ⚠️ Abyss player extraction notice: {e}", flush=True)
            time.sleep(1)
    return None

# =====================================================================
# 🌐 MULTI-SOURCE INTERNET ENGLISH SUBTITLE HUNTER (AnimeTosho, Kitsunekko, SubDL)
# =====================================================================
def extract_candidate_titles(titles, anilist_id=None):
    """Normalizes titles, removes seasons/tags, and fetches synonyms if needed"""
    candidates = []
    for t in (titles or []):
        if not t or not isinstance(t, str): continue
        t = t.strip()
        if t and t not in candidates:
            candidates.append(t)
        clean = re.sub(r'[\(\[\{].*?[\)\]\}]', '', t)
        clean = re.sub(r'[:\-_/\\|]', ' ', clean).strip()
        clean = re.sub(r'\s+', ' ', clean)
        if clean and clean not in candidates:
            candidates.append(clean)
        base = re.sub(r'\b(?:Season|Part|Cour|2nd Season|3rd Season|4th Season)\b.*', '', clean, flags=re.I).strip()
        if base and len(base) > 2 and base not in candidates:
            candidates.append(base)

    if anilist_id and str(anilist_id).isdigit() and len(candidates) < 2:
        try:
            r = requests.post(
                "https://graphql.anilist.co",
                json={"query": "query ($id: Int) { Media (id: $id) { title { romaji english } synonyms } }", "variables": {"id": int(anilist_id)}},
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json().get('data', {}).get('Media', {})
                for k in ['english', 'romaji']:
                    val = (data.get('title') or {}).get(k)
                    if val and val not in candidates:
                        candidates.append(val)
                for syn in data.get('synonyms') or []:
                    if syn and syn not in candidates:
                        candidates.append(syn)
        except Exception: pass

    return candidates

def hunt_from_animetosho(candidate_titles, ep_num, work_dir):
    """Searches AnimeTosho API & extracts direct English subtitle attachments (.ass, .srt)"""
    print(f"[{WORKER_ID}] 🌐 Searching AnimeTosho for Ep {ep_num}...", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    ep_str = f"{int(ep_num):02d}"

    for title in candidate_titles[:3]:
        queries = [
            f"{title} {ep_str}",
            f"{title} - {ep_str}"
        ]
        for q in queries:
            try:
                url = f"https://feed.animetosho.org/json?q={urllib.parse.quote(q)}"
                r = requests.get(url, headers=headers, timeout=12)
                if r.status_code != 200:
                    continue
                items = r.json()
                if not isinstance(items, list) or not items:
                    continue

                for item in items[:10]:
                    attachments = item.get('attachments') or []
                    for att in attachments:
                        att_name = (att.get('name') or att.get('filename') or '').lower()
                        att_url = att.get('url') or att.get('link')
                        if att_url and any(att_name.endswith(ext) for ext in ['.ass', '.srt', '.ssa']):
                            try:
                                dl_r = requests.get(att_url, headers=headers, timeout=20)
                                if dl_r.status_code == 200 and len(dl_r.content) > 500:
                                    ext = att_name.split('.')[-1]
                                    tmp_path = os.path.join(work_dir, f"tosho_sub_{uuid.uuid4().hex[:6]}.{ext}")
                                    with open(tmp_path, 'wb') as f:
                                        f.write(dl_r.content)
                                    if is_valid_sub_file(tmp_path):
                                        enc = detect_encoding(tmp_path)
                                        try: s_obj = pysubs2.load(tmp_path, encoding=enc)
                                        except Exception: s_obj = pysubs2.load(tmp_path, encoding='latin-1')
                                        if len(s_obj.events) >= MIN_SUB_LINE_THRESHOLD:
                                            print(f"[{WORKER_ID}] 🎯 AnimeTosho direct attachment success: {att_name} ({len(s_obj.events)} lines)", flush=True)
                                            return tmp_path
                                        else:
                                            if os.path.exists(tmp_path): os.remove(tmp_path)
                            except Exception: pass

                    page_link = item.get('link')
                    if page_link and 'animetosho.org/view/' in page_link:
                        try:
                            pv_r = requests.get(page_link, headers=headers, timeout=15)
                            if pv_r.status_code == 200:
                                matches = re.findall(r'href="([^"]*(?:storage|storage\.animetosho\.org)[^"]*\.(?:ass|srt|ssa))"', pv_r.text, re.I)
                                for match_url in matches:
                                    full_sub_url = match_url if match_url.startswith('http') else f"https://animetosho.org{match_url}"
                                    dl_r = requests.get(full_sub_url, headers=headers, timeout=20)
                                    if dl_r.status_code == 200 and len(dl_r.content) > 500:
                                        ext = match_url.split('.')[-1].split('?')[0]
                                        tmp_path = os.path.join(work_dir, f"tosho_sub_{uuid.uuid4().hex[:6]}.{ext}")
                                        with open(tmp_path, 'wb') as f:
                                            f.write(dl_r.content)
                                        if is_valid_sub_file(tmp_path):
                                            enc = detect_encoding(tmp_path)
                                            try: s_obj = pysubs2.load(tmp_path, encoding=enc)
                                            except Exception: s_obj = pysubs2.load(tmp_path, encoding='latin-1')
                                            if len(s_obj.events) >= MIN_SUB_LINE_THRESHOLD:
                                                print(f"[{WORKER_ID}] 🎯 AnimeTosho page sub match success ({len(s_obj.events)} lines)!", flush=True)
                                                return tmp_path
                                            else:
                                                if os.path.exists(tmp_path): os.remove(tmp_path)
                        except Exception: pass
            except Exception: pass
    return None

def hunt_from_kitsunekko(candidate_titles, ep_num, work_dir):
    """Scrapes Kitsunekko subtitle archive for anime series and matching episode subtitle"""
    print(f"[{WORKER_ID}] 🌐 Searching Kitsunekko for Ep {ep_num}...", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    ep_pattern = re.compile(r'(?:[ _\.\-\[E]0*' + str(int(ep_num)) + r'[^0-9]|^' + str(int(ep_num)) + r'\b)', re.I)

    try:
        r = requests.get("https://kitsunekko.net/dirlist.php?dir=subtitles%2Fenglish%2F", headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        
        folder_matches = re.findall(r'href="dirlist\.php\?dir=subtitles%2Fenglish%2F([^"]+)%2F"', r.text)
        matched_folders = []
        for raw_folder in folder_matches:
            folder_clean = urllib.parse.unquote(raw_folder).replace('_', ' ').lower()
            for ct in candidate_titles:
                ct_clean = ct.lower()
                if len(ct_clean) >= 4 and (ct_clean in folder_clean or folder_clean in ct_clean):
                    if raw_folder not in matched_folders:
                        matched_folders.append(raw_folder)
                        break

        for folder in matched_folders[:3]:
            folder_url = f"https://kitsunekko.net/dirlist.php?dir=subtitles%2Fenglish%2F{folder}%2F"
            f_resp = requests.get(folder_url, headers=headers, timeout=15)
            if f_resp.status_code != 200:
                continue

            file_links = re.findall(r'href="([^"]*(?:subtitles/english/[^"]*\.(?:ass|srt|zip)))"', f_resp.text, re.I)
            for fl in file_links:
                file_name = urllib.parse.unquote(fl.split('/')[-1])
                if ep_pattern.search(file_name):
                    dl_url = fl if fl.startswith('http') else f"https://kitsunekko.net/{fl.lstrip('/')}"
                    try:
                        dl_r = requests.get(dl_url, headers=headers, timeout=25)
                        if dl_r.status_code == 200 and len(dl_r.content) > 500:
                            if file_name.lower().endswith('.zip'):
                                with zipfile.ZipFile(io.BytesIO(dl_r.content)) as z:
                                    for z_name in z.namelist():
                                        if any(z_name.lower().endswith(ext) for ext in ['.ass', '.srt', '.ssa']):
                                            z_ext = z_name.split('.')[-1]
                                            tmp_path = os.path.join(work_dir, f"kitsu_{uuid.uuid4().hex[:6]}.{z_ext}")
                                            with open(tmp_path, 'wb') as z_out:
                                                z_out.write(z.read(z_name))
                                            if is_valid_sub_file(tmp_path):
                                                enc = detect_encoding(tmp_path)
                                                try: s_obj = pysubs2.load(tmp_path, encoding=enc)
                                                except Exception: s_obj = pysubs2.load(tmp_path, encoding='latin-1')
                                                if len(s_obj.events) >= MIN_SUB_LINE_THRESHOLD:
                                                    print(f"[{WORKER_ID}] 🎯 Kitsunekko zip sub match success: {z_name} ({len(s_obj.events)} lines)!", flush=True)
                                                    return tmp_path
                                                else:
                                                    if os.path.exists(tmp_path): os.remove(tmp_path)
                            else:
                                ext = file_name.split('.')[-1]
                                tmp_path = os.path.join(work_dir, f"kitsu_{uuid.uuid4().hex[:6]}.{ext}")
                                with open(tmp_path, 'wb') as f:
                                    f.write(dl_r.content)
                                if is_valid_sub_file(tmp_path):
                                    enc = detect_encoding(tmp_path)
                                    try: s_obj = pysubs2.load(tmp_path, encoding=enc)
                                    except Exception: s_obj = pysubs2.load(tmp_path, encoding='latin-1')
                                    if len(s_obj.events) >= MIN_SUB_LINE_THRESHOLD:
                                        print(f"[{WORKER_ID}] 🎯 Kitsunekko direct sub match success ({len(s_obj.events)} lines)!", flush=True)
                                        return tmp_path
                                    else:
                                        if os.path.exists(tmp_path): os.remove(tmp_path)
                    except Exception: pass
    except Exception: pass
    return None

def hunt_from_subdl(candidate_titles, ep_num, work_dir):
    """Queries SubDL API for English anime subtitle files"""
    print(f"[{WORKER_ID}] 🌐 Searching SubDL for Ep {ep_num}...", flush=True)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    for title in candidate_titles[:3]:
        try:
            url = f"https://api.subdl.com/api/v1/subtitles?film_name={urllib.parse.quote(title)}&type=tv"
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            res_json = r.json()
            subtitles = res_json.get('subtitles') or []
            if not isinstance(subtitles, list):
                continue

            for sub_item in subtitles:
                s_ep = sub_item.get('episode_number')
                s_lang = (sub_item.get('language') or '').lower()
                if (s_ep == int(ep_num) or str(s_ep) == str(ep_num)) and (s_lang in ['en', 'english']):
                    sub_url = sub_item.get('url') or sub_item.get('subtitles_key')
                    if sub_url:
                        dl_link = sub_url if sub_url.startswith('http') else f"https://dl.subdl.com{sub_url}"
                        dl_r = requests.get(dl_link, headers=headers, timeout=20)
                        if dl_r.status_code == 200 and len(dl_r.content) > 500:
                            if zipfile.is_zipfile(io.BytesIO(dl_r.content)):
                                with zipfile.ZipFile(io.BytesIO(dl_r.content)) as z:
                                    for z_name in z.namelist():
                                        if any(z_name.lower().endswith(ext) for ext in ['.ass', '.srt', '.ssa']):
                                            z_ext = z_name.split('.')[-1]
                                            tmp_path = os.path.join(work_dir, f"subdl_{uuid.uuid4().hex[:6]}.{z_ext}")
                                            with open(tmp_path, 'wb') as z_out:
                                                z_out.write(z.read(z_name))
                                            if is_valid_sub_file(tmp_path):
                                                enc = detect_encoding(tmp_path)
                                                try: s_obj = pysubs2.load(tmp_path, encoding=enc)
                                                except Exception: s_obj = pysubs2.load(tmp_path, encoding='latin-1')
                                                if len(s_obj.events) >= MIN_SUB_LINE_THRESHOLD:
                                                    print(f"[{WORKER_ID}] 🎯 SubDL zip sub match success ({len(s_obj.events)} lines)!", flush=True)
                                                    return tmp_path
                                                else:
                                                    if os.path.exists(tmp_path): os.remove(tmp_path)
                            else:
                                tmp_path = os.path.join(work_dir, f"subdl_{uuid.uuid4().hex[:6]}.srt")
                                with open(tmp_path, 'wb') as f:
                                    f.write(dl_r.content)
                                if is_valid_sub_file(tmp_path):
                                    enc = detect_encoding(tmp_path)
                                    try: s_obj = pysubs2.load(tmp_path, encoding=enc)
                                    except Exception: s_obj = pysubs2.load(tmp_path, encoding='latin-1')
                                    if len(s_obj.events) >= MIN_SUB_LINE_THRESHOLD:
                                        print(f"[{WORKER_ID}] 🎯 SubDL direct sub match success ({len(s_obj.events)} lines)!", flush=True)
                                        return tmp_path
                                    else:
                                        if os.path.exists(tmp_path): os.remove(tmp_path)
        except Exception: pass
    return None

def hunt_english_sub_from_web(titles, ep_num, anilist_id=None, work_dir="."):
    """Multi-source orchestrator: tries AnimeTosho -> Kitsunekko -> SubDL"""
    candidate_titles = extract_candidate_titles(titles, anilist_id=anilist_id)
    print(f"[{WORKER_ID}] 🕵️ HUNTING WEB ENGLISH SUBS for Ep {ep_num} across candidate titles: {candidate_titles[:4]}", flush=True)

    # 1. AnimeTosho
    res = hunt_from_animetosho(candidate_titles, ep_num, work_dir)
    if res:
        return res

    # 2. Kitsunekko
    res = hunt_from_kitsunekko(candidate_titles, ep_num, work_dir)
    if res:
        return res

    # 3. SubDL
    res = hunt_from_subdl(candidate_titles, ep_num, work_dir)
    if res:
        return res

    print(f"[{WORKER_ID}] ⚠️ Web Sub Hunter exhausted all sources without finding English subtitle for Ep {ep_num}.", flush=True)
    return None

# =====================================================================
# 📋 REPORT JOB EXECUTION (REPORTER ENGINE)
# =====================================================================
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

    abyss_vid = data.get('links', {}).get('abyss_video_id')
    if not abyss_vid and data.get('links', {}).get('abyss_embed'):
        m_ab = re.search(r'embed/([a-zA-Z0-9_-]+)', str(data.get('links', {}).get('abyss_embed')))
        if m_ab: abyss_vid = m_ab.group(1)

    acc_hint = data.get('links', {}).get('account') or data.get('account_name')

    server = data.get('server', 1)
    preferred_token = RPM_API_TOKEN_2 if str(server) == "2" else RPM_API_TOKEN
    series_ref = ep_ref.parent.parent if ep_ref.parent else None
    anime_id = series_ref.id if series_ref else data.get('anilist_id', 0)
    ep_num = data.get('episodeNumber', 1)
    job_id = payload.get("job_id") or f"report_{payload.get('doc_id')}"

    # Step 1: Try to extract subtitle file from RPM video
    sub_file = None
    working_token = preferred_token
    if rpm_id:
        sub_res = download_rpm_sub_api(rpm_id, api_token=preferred_token, work_dir=work_dir, return_token=True)
        if isinstance(sub_res, tuple):
            sub_file, working_token = sub_res
        else:
            sub_file, working_token = sub_res, preferred_token

    # Step 2: If no sub inside video, HUNT FROM WEB!
    if not sub_file:
        print(f"[{WORKER_ID}] ⚠️ No subtitle track inside RPM video. Initiating Web Subtitle Hunt...", flush=True)
        candidate_titles = []
        if payload.get("title"): candidate_titles.append(payload.get("title"))
        if payload.get("romaji_title"): candidate_titles.append(payload.get("romaji_title"))
        if data.get("episodeTitle"): candidate_titles.append(data.get("episodeTitle"))
        if series_ref:
            try:
                s_snap = series_ref.get()
                if s_snap.exists:
                    s_data = s_snap.to_dict() or {}
                    t_obj = s_data.get('title') if isinstance(s_data.get('title'), dict) else {}
                    for k in ['name_english', 'name_romaji']:
                        if s_data.get(k): candidate_titles.append(s_data[k])
                    if isinstance(t_obj, dict):
                        for k in ['english', 'romaji']:
                            if t_obj.get(k): candidate_titles.append(t_obj[k])
            except Exception: pass

        web_sub = hunt_english_sub_from_web(candidate_titles, ep_num, anilist_id=anime_id, work_dir=work_dir)
        if web_sub:
            sub_file = web_sub

    if not sub_file:
        print(f"[{WORKER_ID}] ❌ No valid subtitle file in video or web for {ep_path}", flush=True)
        ep_ref.update({
            'report_status': 'failed_no_sub_available',
            'report_status_server_1': 'failed_no_sub',
            'report_status_server_2': 'failed_no_sub',
            'worker_id': None
        })
        try:
            rtdb.reference(f"sub_manager_jobs/completed_jobs/{job_id}").set({
                "status": "completed",
                "result": "failed_no_sub",
                "ep_path": ep_path,
                "timestamp": int(time.time() * 1000)
            })
        except Exception: pass
        return False

    active_token = working_token or preferred_token
    determined_server = 2 if (active_token and RPM_API_TOKEN_2 and active_token == RPM_API_TOKEN_2) else 1

    rel_ctx = {}
    out_en = os.path.join(work_dir, f"english_sub_{uuid.uuid4().hex[:4]}.srt")
    out_si = os.path.join(work_dir, f"sinhala_sub_{uuid.uuid4().hex[:4]}.srt")

    eng_sub = process_english_sub(sub_file, out_name=out_en, log_prefix=f"[{WORKER_ID}]")
    github_en = upload_to_github_release(eng_sub, asset_name="English.srt", release_context=rel_ctx) if eng_sub else None

    sin_sub = process_sinhala_sub(sub_file, out_name=out_si, max_workers=5, log_prefix=f"[{WORKER_ID}]")
    github_si = None

    if sin_sub:
        github_si = upload_to_github_release(sin_sub, asset_name="Sinhala.srt", release_context=rel_ctx)
        
        # 1. Attach to RPM (Server 2)
        if rpm_id:
            delete_existing_sinhala_subs(rpm_id, api_token=active_token)
            upload_sub_to_rpm(rpm_id, sin_sub, api_token=active_token, remote_url=github_si)
        
        # 2. Attach to Abyss (Server 1)
        if abyss_vid:
            try:
                with open(sin_sub, "rb") as f_sin:
                    sin_bytes = f_sin.read()
                replace_abyss_sinhala_subtitle(abyss_vid, sin_bytes, account_hint=acc_hint)
            except Exception as e_ab:
                print(f"[{WORKER_ID}] ⚠️ Error pushing sub to Abyss: {e_ab}", flush=True)

    if github_si:
        ep_ref.update({
            'status': 'uploaded',
            'report_status': 'fixed',
            'report_status_server_1': 'fixed',
            'report_status_server_2': 'fixed',
            'subtitles.sinhala': github_si,
            'subtitles.english': github_en if github_en else 'not_found',
            'last_fixed': firestore.SERVER_TIMESTAMP,
            'last_fixed_server_1': firestore.SERVER_TIMESTAMP,
            'last_fixed_server_2': firestore.SERVER_TIMESTAMP,
            'worker_id': None,
            'server': determined_server
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
        ep_ref.update({
            'report_status': 'failed_translation_or_empty',
            'report_status_server_1': 'failed_translation',
            'report_status_server_2': 'failed_translation',
            'worker_id': None
        })
        try:
            rtdb.reference(f"sub_manager_jobs/completed_jobs/{job_id}").set({
                "status": "completed",
                "result": "failed_translation",
                "ep_path": ep_path,
                "timestamp": int(time.time() * 1000)
            })
        except Exception: pass
        return False

# =====================================================================
# 🔍 HUNT JOB EXECUTION (AUTO SUB HUNTER ENGINE)
# =====================================================================
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
    preferred_token = RPM_API_TOKEN_2 if str(server) == "2" else RPM_API_TOKEN

    if not rpm_id:
        stream_url = data.get('links', {}).get('rpm_stream') or data.get('links', {}).get('rpm_download') or ""
        m = re.search(r'/(?:v|d|embed)/([a-zA-Z0-9_-]+)', stream_url)
        if m: rpm_id = m.group(1)
        else:
            m2 = re.search(r'rpmshare\.com/v([a-zA-Z0-9_-]+)', stream_url)
            if m2: rpm_id = m2.group(1)

    abyss_vid = data.get('links', {}).get('abyss_video_id')
    if not abyss_vid and data.get('links', {}).get('abyss_embed'):
        val_embed = data.get('links', {}).get('abyss_embed')
        if val_embed and isinstance(val_embed, str):
            m_ab = re.search(r'embed/([a-zA-Z0-9_-]+)', val_embed)
            if m_ab: abyss_vid = m_ab.group(1)

    acc_hint = data.get('links', {}).get('account') or data.get('account_name')

    series_ref = ep_ref.parent.parent if ep_ref.parent else None
    anime_id = series_ref.id if series_ref else data.get('anilist_id', 0)
    ep_num = data.get('episodeNumber', 1)
    ep_title = data.get('episodeTitle') or f"Episode {ep_num}"

    ep_ref.update({'subtitles.sinhala': 'processing_cloud'})
    print(f"[{WORKER_ID}] 🚀 Starting Hunt: {ep_title} (Ep {ep_num})", flush=True)

    si_path = None
    en_path = None
    active_token = preferred_token

    if rpm_id:
        dual_res = fetch_dual_subs_from_rpm(rpm_id, preferred_token, work_dir=work_dir, return_token=True)
        if isinstance(dual_res, tuple) and len(dual_res) == 3:
            si_path, en_path, working_token = dual_res
            active_token = working_token or preferred_token
        else:
            si_path, en_path = dual_res

    # Check Abyss Video fallback if no Sinhala sub yet
    if not si_path and abyss_vid:
        print(f"[{WORKER_ID}] ⚡ [Abyss Fallback] Checking Abyss ID: {abyss_vid} for Sinhala sub...", flush=True)
        sub_bytes = download_sinhala_sub_from_abyss(abyss_vid)
        if sub_bytes:
            out_si_ab = os.path.join(work_dir, f"sinhala_abyss_{uuid.uuid4().hex[:4]}.srt")
            with open(out_si_ab, "wb") as f_sub:
                f_sub.write(sub_bytes)
            si_path = out_si_ab

    # IF STILL NO SUB -> HUNT FROM WEB!
    if not si_path and not en_path:
        print(f"[{WORKER_ID}] 🌐 No subtitles on RPM or Abyss. Hunting English subtitle from Web for {ep_title} (Ep {ep_num})...", flush=True)
        candidate_titles = []
        if payload.get("title"): candidate_titles.append(payload.get("title"))
        if payload.get("romaji_title"): candidate_titles.append(payload.get("romaji_title"))
        if ep_title: candidate_titles.append(ep_title)
        if series_ref:
            try:
                s_snap = series_ref.get()
                if s_snap.exists:
                    s_data = s_snap.to_dict() or {}
                    t_obj = s_data.get('title') if isinstance(s_data.get('title'), dict) else {}
                    for k in ['name_english', 'name_romaji']:
                        if s_data.get(k): candidate_titles.append(s_data[k])
                    if isinstance(t_obj, dict):
                        for k in ['english', 'romaji']:
                            if t_obj.get(k): candidate_titles.append(t_obj[k])
            except Exception: pass

        web_sub = hunt_english_sub_from_web(candidate_titles, ep_num, anilist_id=anime_id, work_dir=work_dir)
        if web_sub:
            en_path = web_sub

    determined_server = 2 if (active_token and RPM_API_TOKEN_2 and active_token == RPM_API_TOKEN_2) else 1
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
                if rpm_id:
                    delete_existing_sinhala_subs(rpm_id, active_token)
                    upload_sub_to_rpm(rpm_id, si_gen_path, active_token, remote_url=final_si_url)
                if abyss_vid:
                    try:
                        with open(si_gen_path, "rb") as f_sin:
                            sin_bytes = f_sin.read()
                        replace_abyss_sinhala_subtitle(abyss_vid, sin_bytes, account_hint=acc_hint)
                    except Exception as e_ab:
                        print(f"[{WORKER_ID}] ⚠️ Error pushing sub to Abyss: {e_ab}", flush=True)

    if si_path:
        print(f"[{WORKER_ID}] ⭐ Direct Sinhala Sub Ready!", flush=True)
        si_clean_path = process_sinhala_sub(
            si_path,
            out_name=os.path.join(work_dir, f"sinhala_{uuid.uuid4().hex[:4]}.srt"),
            max_workers=5,
            log_prefix=f"[{WORKER_ID}]"
        )
        target_si = si_clean_path if si_clean_path else si_path
        final_si_url = upload_to_github_release(target_si, asset_name="Sinhala.srt", release_context=rel_ctx)
        if rpm_id:
            delete_existing_sinhala_subs(rpm_id, active_token)
            upload_sub_to_rpm(rpm_id, target_si, active_token, remote_url=final_si_url)
        if abyss_vid:
            try:
                with open(target_si, "rb") as f_sin:
                    sin_bytes = f_sin.read()
                replace_abyss_sinhala_subtitle(abyss_vid, sin_bytes, account_hint=acc_hint)
            except Exception as e_ab:
                print(f"[{WORKER_ID}] ⚠️ Error pushing sub to Abyss: {e_ab}", flush=True)

    job_id = payload.get("job_id") or f"hunt_{payload.get('doc_id')}"
    updates = {'last_auto_update': firestore.SERVER_TIMESTAMP, 'server': determined_server}
    if final_si_url:
        updates['subtitles.sinhala'] = final_si_url
        updates['status'] = 'uploaded'
        updates['report_status_server_1'] = 'fixed'
        updates['report_status_server_2'] = 'fixed'
        updates['report_status'] = 'fixed'
        updates['last_fixed_server_1'] = firestore.SERVER_TIMESTAMP
        updates['last_fixed_server_2'] = firestore.SERVER_TIMESTAMP
        clear_missing_sub_alert(rtdb, anime_id, ep_num)
        print(f"[{WORKER_ID}] ✅ Hunt SUCCESS! Sinhala Sub Online: {final_si_url}", flush=True)
    else:
        updates['subtitles.sinhala'] = 'no_sub_available'
        updates['hunt_status'] = 'no_sub_available'
        push_missing_sub_alert(rtdb, anime_id, ep_title, ep_num, rpm_id, server)
        print(f"[{WORKER_ID}] ⚠️ Hunt COMPLETED: No subtitle available. Marked 'no_sub_available'.", flush=True)

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
