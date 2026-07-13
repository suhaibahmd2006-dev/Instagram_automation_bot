import os
import time
import re
import json
import random
import logging
import requests
from pathlib import Path
from instagrapi import Client
from yt_dlp import YoutubeDL
import shutil
try:
    import fishaudio
    from fishaudio import FishAudio
    FISH_AVAILABLE = True
except ImportError:
    FISH_AVAILABLE = False
    print("⚠️ Fish Audio not installed. Run: pip install fish-audio-sdk")

try:
    from PIL import Image
    import io
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    logging.warning("⚠️ Pillow not installed. Image re-encoding disabled. Run: pip install Pillow")

# ── Logging ──
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
# Suppress noisy instagrapi/httpx request logs (hides IP, device info)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('instagrapi').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

TEMP_IMAGE_DIR = "temp_images"
os.makedirs(TEMP_IMAGE_DIR, exist_ok=True)

# ── CONFIGURATION ──
# 🔑 Instagram Login – either username/password OR session ID
IG_USERNAME = "USERNAME"
IG_PASSWORD = "PASSWORD"
SESSION_ID = "ENTER_YOUR_SEESION_ID"

# 🔑 Hugging Face API Token (Free)
# Get your token from: https://huggingface.co/settings/tokens
HF_TOKEN = "HUGGING FACE API"

TARGET_CHATS = [
   "CHATS_URL"
]
THREAD_IDS = [url.split('/t/')[-1].replace('/', '') for url in TARGET_CHATS]

# ── AI via Groq API (FREE — ultra fast inference) ──
GROQ_API_KEY = "GROK_API"

# ── Fish Audio TTS ──
FISH_API_KEY = "FISH_API"
FISH_MODEL  = "MODEL"   # S2 Pro — best free model on Fish Audio

# Groq models to try in order
AI_MODELS = [
    "MODELNAME",   # Best quality — Llama 3.3 70B
    "FALLBACK_MODEL",            # Fast fallback — Llama 3 8B
    "MIXTRAL_FALLBACK_MODEL",        # Mixtral fallback
]

def call_ai(system_prompt: str, user_message: str, max_tokens: int = 300) -> str:
    """Call Groq API — free, ultra-fast inference."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message}
    ]
    return call_ai_with_history(messages, max_tokens)

def call_ai_with_history(messages: list, max_tokens: int = 300) -> str:
    """Call Groq API with full message history."""
    for model in AI_MODELS:
        try:
            logging.info(f"🤖 Calling Groq model: {model}")
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.7
                },
                timeout=30
            )
            logging.info(f"📡 Groq API status: {response.status_code}")
            if response.status_code != 200:
                logging.error(f"❌ Groq error {response.status_code}: {response.text[:300]}")
                continue
            data = response.json()
            if "error" in data:
                logging.error(f"❌ Groq returned error: {data['error']}")
                continue
            content = data["choices"][0]["message"]["content"]
            logging.info(f"✅ AI replied ({model}): {content[:80]}...")
            return content
        except Exception as e:
            logging.error(f"❌ Exception with {model}: {e}")
            continue
    return None

# ── Temp folder for audio/images ──
TEMP_AUDIO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")
os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)

# ── Session Cache path ──
SESSION_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session.json")

# ── Cooldowns ──
music_cooldown = {}
vn_cooldown = {}
COOLDOWN_SECONDS = 15

# ── Tracking ──
last_seen_ids = {thread_id: None for thread_id in THREAD_IDS}
sent_replies = []
processed_ids = {thread_id: set() for thread_id in THREAD_IDS}

# ── Conversation Context ──
# After using -ai, users stay in AI mode for this many seconds
AI_SESSION_TIMEOUT = 120  # 2 minutes — resets after each message
# {(thread_id, user_id): last_active_timestamp}
ai_active_users = {}
# {thread_id: [{role, content}, ...]} — conversation history per thread
convo_history = {}

# ── AI System Prompt ──
AI_SYSTEM_PROMPT = (
    "You are a friendly, helpful, and respectful AI assistant who replies in Urdu (Roman Urdu is fine). "
    "Always be kind, polite, and supportive no matter how the user speaks to you. "
    "Never use any slurs, insults, profanity, or offensive language under any circumstances. "
    "If someone asks something rude or inappropriate, gently redirect them. "
    "You can be casual and fun but always stay respectful and decent. "
    "Never generate, describe, or discuss any sexual, nude, or adult content — firmly but politely refuse such requests. "
    "Keep replies concise, clear, and helpful. Use friendly emojis occasionally like 😊, 👍, 🙌, ✨."
)

# ── Cybersecurity / Hacking Expert Prompt ──
HACK_SYSTEM_PROMPT = (
    "You are a cybersecurity expert and ethical hacker with deep knowledge of offensive and defensive security. "
    "You provide detailed, technical, and educational information about cybersecurity topics including: "
    "network attacks (DDoS, MITM, ARP spoofing), hacking techniques (SQL injection, XSS, phishing, brute force, social engineering), "
    "IP tracking and anonymization (VPNs, proxies, Tor, IP geolocation), "
    "penetration testing, vulnerability scanning, exploit development, "
    "firewall bypass, Wi-Fi hacking (WPA2, Evil Twin), password cracking, "
    "how to protect and secure systems, and how attacks are executed technically. "
    "Always include both the attack method AND its defense/protection. "
    "Reply in a clear, structured format with steps where needed. "
    "Use Roman Urdu. "
    "This is strictly for educational and ethical purposes only — always add a disclaimer when discussing attacks."
)

# ── Instagrapi client ──
client = Client()

print("🔑 Logging in via instagrapi...")
logged_in = False

# 1. Try to load cached session first
if os.path.exists(SESSION_SETTINGS_PATH):
    try:
        client.load_settings(SESSION_SETTINGS_PATH)
        client.get_timeline_feed()
        print("✅ Logged in using cached session settings (session.json)")
        logged_in = True
    except Exception:
        logging.warning("Cached session expired/invalid. Re-initializing client for a clean login...")
        # Re-initialize the client to completely wipe out any cached corrupted cookies
        client = Client()
        logged_in = False

# 2. Try Session ID login
if not logged_in and SESSION_ID and SESSION_ID != "YOUR_SESSION_ID_HERE":
    try:
        print("Trying to log in using SESSION_ID...")
        client.login_by_sessionid(SESSION_ID)
        print(f"✅ Logged in using session ID (user_id: {client.user_id})")
        logged_in = True
        client.dump_settings(SESSION_SETTINGS_PATH)
        print("💾 Session settings saved to session.json")
    except Exception as e:
        logging.warning(f"Session ID login failed ({e}). Falling back to username/password...")

# 3. Try Username/Password login as final fallback
if not logged_in:
    try:
        print(f"Trying to log in as {IG_USERNAME} using password...")
        client.login(IG_USERNAME, IG_PASSWORD)
        print(f"✅ Logged in as {IG_USERNAME} (user_id: {client.user_id})")
        logged_in = True
        client.dump_settings(SESSION_SETTINGS_PATH)
        print("💾 Session settings saved to session.json")
    except Exception as e:
        print(f"❌ Username/password login failed: {e}")
        print("\n⚠️ Try these fixes:")
        print("1. Get a fresh session ID from your browser and paste it into SESSION_ID variable.")
        print("2. If still failing, your IP might be blocked – use a VPN or mobile data.")
        exit(1)

# ═══════════════════════════════════════════════════════════════
# 🎵 MUSIC STICKER FUNCTIONS (same as before)
# ═══════════════════════════════════════════════════════════════

GRAPHQL_URL = "https://www.instagram.com/api/graphql"
IG_WEB_APP_ID = "WEB_APP_ID"
REQUEST_TIMEOUT = 30

COMET_AV = "17841417100740600"
COMET_HS = "20638.HYP:instagram_web_pkg.2.1...0"
COMET_REV = "1042629546"
COMET_HSI = "7658669625254534644"
COMET_DYN = "7xeUjG1mxu1syaxG4Vp41twpUnwgU7SbzEdF8aUco2qwJyEiw9-1DwUx609vCwjE1EEc87m0yE462mcw5Mx62G5UswoEcE7O2l0Fwqo5W1yw9O1lwxwQzXwae4UaEW2G0AEco5G0zK5o4q1qwl81wEbUGdwtUeo9UaQ0Lo6-bwHwKG1pg2fwxyo6O1FwlAcwBwUQp6x6U42UnAwCAxW1oxe6U5q0EoKmUhw4rwXyEcE4y16wAwj83KwRyrg"
COMET_CRN = "comet.igweb.PolarisDirectInboxRoute"

SEND_S = "sf6vvb:4iobru:4g6c80"
SEND_CSR = "gJd1T95P9v79Y_cIV3shkVbpuA8b98YwmfRT_8rikL_h3aGRaFrABuGWFeyd4ytRHa-GmTABy4VGKgyOsDZVlpTRoFfEyu8hArHi8GviFqBACGmyCgKGjlau8HyfWzDAJzuQKiK9yEysx4A9gKdCBCCgyvKin9hUzDBzUNLizpkZ4jzBTAQnF6BhASKHAgSmueGAu468HAx-ahVFEOmbFoCieXK8ghpoPCh238xAAztGFbAVUdE2ow3T8057W01cMw0chm1wxO0eoU0gie0vy0JF98mwhm0d2a0GU7iu0z8O0f8gjxC8w5lw4p83u0KV84O5obU4C0CUzo0yy35aayk5Yw1780EOpPwumbAhAVt0CwtEvoiALoqw3CokwSo05oi0aHw0z2o1k85G1sw5IwYw3YE0tEIM1S8aQ2V7ki"
SEND_HSDP = "g4DZ86yBpqEAga6GTQSy-sFlkdHchF4EEibzh8wXxFaz4i-Ax-oiA8cV8LEUjggAxC7x0IC1om79Emz8V2Q0zOxaVSl2AmezGxW8O93A3Kp7gy4985q216xy1Sxui2aezof898co982zJ16EJ1mfAxC261Px63W2iifAx210CyUjAw_w5vwt8d80Ja0488cU2sxO3u064815u1xw5Sw4Hwai0giew5pw2iQ0d6wde15w9O3Vafg6i0yo1TU9Hw"
SEND_HBLP = "4wg85-78eUG6E9omghKUyfxm4EqguwVzoyl39UCiucAxJ1rg5DDCyUkyEa8O1_iKUCVe8VVAmHWl2-E-eKQmdJxa7KbhGhRzAUG5oc-fx16VEyqiEKfx64EqDJ5xuibxquqmu22aybKq7oW2adglx-2qiaV84mRKqgxaJ5DDzWUpzVeFA1PByUfE8QiEOQ498kwzGHyUF0OzoK1nyo7S1_wRCCK545Edaw5gwsE4yu1Zx-78do1zo3mw5AwwwPw9O78dU0PW17Bg1CE4u1IwOws820wzUcEbE5ifwo8ek322C6EW0VUde0Eei0_UW0iy5ECfw44w56g3zw2e8aoszQ0wUmyEcojw9N0Cxpafg6i1CxfUG69GxhBDU-36m3a59U2bU6-2qU"
SEND_SJSP = "g4DZ8D3QsFmmHp2yxGJZdELDall3qRp6Fqa4DUQg4UqiEN4ihctC4F23eibWe2mi6ou42-E5xo4u6U2gxG0x8eVAu9x20iO14w0oEU"
SEND_QPL_FLOW_IDS = "354954279,67975436"

_req_counter = 80
_last_request_time = 0

def _next_req_id():
    global _req_counter
    _req_counter += 1
    return str(_req_counter)

def get_attr(obj, attr, default=None):
    try:
        return getattr(obj, attr, default)
    except:
        return default

def human_like_delay(min_sec=1.0, max_sec=3.0):
    time.sleep(random.uniform(min_sec, max_sec))

def ensure_request_gap(min_gap=2.0):
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < min_gap:
        time.sleep(min_gap - elapsed + random.uniform(0, 0.5))
    _last_request_time = time.time()

def _get_session_cookie(cl: Client):
    try:
        if hasattr(cl, 'cookies') and cl.cookies:
            if isinstance(cl.cookies, dict):
                if 'sessionid' in cl.cookies:
                    return cl.cookies['sessionid'], "cookies.dict"
            else:
                for cookie in cl.cookies:
                    if cookie.name == "sessionid":
                        return cookie.value, "cookies"
    except:
        pass
    try:
        if hasattr(cl, 'private') and hasattr(cl.private, 'cookies'):
            if isinstance(cl.private.cookies, dict):
                if 'sessionid' in cl.private.cookies:
                    return cl.private.cookies['sessionid'], "private.cookies.dict"
            else:
                for cookie in cl.private.cookies:
                    if cookie.name == "sessionid":
                        return cookie.value, "private.cookies"
    except:
        pass
    try:
        if hasattr(cl, 'sessionid') and cl.sessionid:
            return cl.sessionid, "attribute"
    except:
        pass
    return None, None

def _get_fb_dtsg_and_lsd(cl: Client):
    try:
        human_like_delay(1.5, 3.5)
        ensure_request_gap(2.0)
        sessionid, _ = _get_session_cookie(cl)
        if not sessionid:
            return None, None
        session = requests.Session()
        session.cookies.set("sessionid", sessionid, domain=".instagram.com")
        csrf_token = None
        try:
            for cookie in cl.private.cookies:
                if cookie.name == "csrftoken":
                    csrf_token = cookie.value
                    break
        except:
            pass
        if csrf_token:
            session.cookies.set("csrftoken", csrf_token, domain=".instagram.com")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        response = session.get(
            "https://www.instagram.com/direct/inbox/",
            headers=headers,
            timeout=15
        )
        html = response.text
        fb_dtsg = None
        patterns = [
            r'"fb_dtsg":"([^"]+)"',
            r'"DTSGInitialData",\s*\[\],\s*{\s*"token":"([^"]+)"',
            r'"f\+/BAn"\s*:\s*"([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                fb_dtsg = match.group(1)
                break
        lsd = None
        patterns = [
            r'"LSD",\s*\[\],\s*{\s*"token":"([^"]+)"',
            r'"lsd":"([^"]+)"',
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                lsd = match.group(1)
                break
        return fb_dtsg, lsd
    except Exception as e:
        logging.error(f"Failed to get fb_dtsg/lsd: {e}")
        return None, None

def send_music_sticker_graphql_api(cl: Client, thread_id: str, track) -> bool:
    try:
        title = get_attr(track, "title") or get_attr(track, "display_title") or "Unknown title"
        artist = get_attr(track, "display_artist") or get_attr(track, "subtitle") or "Unknown artist"
        audio_cluster_id = get_attr(track, "audio_cluster_id") or get_attr(track, "id") or ""
        if not audio_cluster_id:
            logging.error("Missing audio cluster id")
            return False
        human_like_delay(0.5, 1.5)
        ensure_request_gap(1.5)
        sessionid, _ = _get_session_cookie(cl)
        if not sessionid:
            logging.error("No sessionid available")
            return False
        fb_dtsg, lsd = _get_fb_dtsg_and_lsd(cl)
        if not fb_dtsg or not lsd:
            logging.error("Failed to get fb_dtsg or lsd")
            return False
        logging.info(f"Got fb_dtsg: {fb_dtsg[:20]}...")
        logging.info(f"Got lsd: {lsd}")
        csrf_token = None
        try:
            for cookie in cl.private.cookies:
                if cookie.name == "csrftoken":
                    csrf_token = cookie.value
                    break
        except:
            pass
        jazoest = "2" + str(sum(ord(c) for c in fb_dtsg))
        variables_obj = {
            "send_data": {
                "thread_id": str(thread_id),
                "offline_threading_id": str(int(time.time() * 1000))
            },
            "data": {
                "audio_asset_id": str(audio_cluster_id)
            }
        }
        analytics_tags = [f"qpl_active_flow_ids={SEND_QPL_FLOW_IDS}"]
        form_data = {
            "av": COMET_AV,
            "__d": "www",
            "__user": "0",
            "__a": "1",
            "__req": _next_req_id(),
            "__hs": COMET_HS,
            "dpr": "1",
            "__ccg": "GOOD",
            "__rev": COMET_REV,
            "__s": SEND_S,
            "__hsi": COMET_HSI,
            "__dyn": COMET_DYN,
            "__csr": SEND_CSR,
            "__hsdp": SEND_HSDP,
            "__hblp": SEND_HBLP,
            "__sjsp": SEND_SJSP,
            "__comet_req": "7",
            "fb_dtsg": fb_dtsg,
            "jazoest": jazoest,
            "lsd": lsd,
            "__spin_r": COMET_REV,
            "__spin_b": "trunk",
            "__spin_t": str(int(time.time())),
            "__crn": COMET_CRN,
            "qpl_active_flow_ids": SEND_QPL_FLOW_IDS,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "IGDirectMusicStickerShareMutation",
            "server_timestamps": "true",
            "doc_id": "26883421864608852",
            "variables": json.dumps(variables_obj),
            "fb_api_analytics_tags": json.dumps(analytics_tags),
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://www.instagram.com/direct/inbox/",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "*/*",
            "Connection": "keep-alive",
            "x-ig-app-id": IG_WEB_APP_ID,
            "x-asbd-id": "359341",
        }
        if csrf_token:
            headers["x-csrftoken"] = csrf_token
        session = requests.Session()
        session.cookies.set("sessionid", sessionid, domain=".instagram.com")
        if csrf_token:
            session.cookies.set("csrftoken", csrf_token, domain=".instagram.com")
        doc_ids = ["26883421864608852", "26548947361463418"]
        for doc_id in doc_ids:
            try:
                human_like_delay(0.5, 1.5)
                ensure_request_gap(1.0)
                form_data["doc_id"] = doc_id
                logging.info(f"Sending request with doc_id: {doc_id}")
                response = session.post(
                    GRAPHQL_URL,
                    data=form_data,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )
                text = response.text
                if text.startswith('for (;;);'):
                    text = text[9:]
                logging.info(f"Response status: {response.status_code}")
                logging.info(f"Response preview: {text[:200]}")
                result = json.loads(text)
                if "errors" not in result:
                    logging.info(f"✅ Music sticker sent successfully with doc_id {doc_id}")
                    return True
                else:
                    error_msg = result.get("errors", [{}])[0].get("message", "Unknown")
                    logging.warning(f"GraphQL error: {error_msg}")
            except json.JSONDecodeError:
                logging.warning("Non-JSON response")
                continue
            except Exception as e:
                logging.warning(f"Error with doc_id {doc_id}: {e}")
                continue
        return False
    except Exception as e:
        logging.error(f"GraphQL music sticker send failed: {e}")
        return False

def search_track(cl: Client, query: str):
    try:
        human_like_delay(1.0, 2.5)
        ensure_request_gap(1.5)
        tracks = cl.search_music(query)
    except Exception as e:
        logging.error(f"Search failed: {e}")
        return None
    if not tracks:
        return None
    track = tracks[0]
    audio_asset_id = getattr(track, "id", None)
    if not audio_asset_id:
        return None
    title = getattr(track, "title", None) or query
    artist = getattr(track, "display_artist", None) or "Unknown Artist"
    return track, str(audio_asset_id), title, artist

def get_itunes_fallback(query: str) -> str:
    ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
    try:
        human_like_delay(0.5, 1.5)
        ensure_request_gap(1.0)
        response = requests.get(
            ITUNES_SEARCH_URL,
            params={"term": query, "media": "music", "entity": "song", "limit": 1},
            timeout=15,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except Exception as e:
        logging.error(f"iTunes fallback failed: {e}")
        return f"❌ No results found for {query}"
    if not results:
        return f"❌ No results found for {query}"
    track = results[0]
    preview_url = track.get("previewUrl", "")
    track_view_url = track.get("trackViewUrl", "")
    response_text = f"🎵 {track.get('trackName', query)} — {track.get('artistName', '')}\n"
    if preview_url:
        response_text += f"🎧 [Preview] {preview_url}\n"
    if track_view_url:
        response_text += f"🔗 [Apple Music] {track_view_url}"
    return response_text

def handle_music_command(query: str, thread_id: str) -> str:
    if not query:
        return "⚠️ Please specify a song.\nExample: -music Blinding Lights"
    last = music_cooldown.get(thread_id)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < COOLDOWN_SECONDS:
            return f"⏳ Slow down! Try again in {round(COOLDOWN_SECONDS - elapsed, 1)}s."
    music_cooldown[thread_id] = time.monotonic()

    logging.info(f"🔍 Searching for music: {query}")
    result = search_track(client, query)
    if not result:
        return f"❌ No results found for {query}"
    track, audio_asset_id, title, artist = result
    logging.info(f"🎵 Found: {title} by {artist} (ID: {audio_asset_id})")
    logging.info("📤 Sending music sticker...")
    try:
        sent = send_music_sticker_graphql_api(client, thread_id, track)
        if sent:
            logging.info("✅ Music sticker sent!")
            return None
    except Exception as e:
        logging.error(f"GraphQL send failed: {e}")
    logging.info("ℹ️ Using iTunes fallback")
    return get_itunes_fallback(query)

# ──────────────────────────────────────────────────────────────
# 🎤 VOICE NOTE VIA instagrapi's direct_send_voice
# ──────────────────────────────────────────────────────────────

def download_audio_for_voice(query: str) -> str:
    os.makedirs(TEMP_AUDIO_DIR, exist_ok=True)
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(TEMP_AUDIO_DIR, '%(id)s.%(ext)s'),
        'default_search': 'ytsearch1',
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '128',
        }],
        'quiet': True,
        'no_warnings': True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        if 'entries' in info:
            info = info['entries'][0]
        base = info['id']
        audio_path = os.path.join(TEMP_AUDIO_DIR, f"{base}.m4a")
        if os.path.exists(audio_path):
            return audio_path
        for f in os.listdir(TEMP_AUDIO_DIR):
            if f.startswith(base) and f.endswith(('.m4a', '.mp3', '.aac')):
                return os.path.join(TEMP_AUDIO_DIR, f)
        return None

def handle_vn_command(query: str, thread_id: str) -> str:
    if not query:
        return "⚠️ Mention a song name! Example: -vn Starboy"
    last = vn_cooldown.get(thread_id)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < COOLDOWN_SECONDS:
            return f"⏳ Slow down! Try again in {round(COOLDOWN_SECONDS - elapsed, 1)}s."
    vn_cooldown[thread_id] = time.monotonic()

    logging.info(f"🎤 Downloading audio for voice note: {query}")
    audio_path = download_audio_for_voice(query)
    if not audio_path:
        return f"❌ Could not download audio for '{query}'."

    try:
        client.direct_send_voice(audio_path, thread_ids=[int(thread_id)])
        logging.info(f"✅ Voice note sent to thread {thread_id}")
        return None
    except Exception as e:
        logging.error(f"Failed to send voice note: {e}")
        return f"❌ Failed to send voice note: {e}"
    finally:
        try:
            os.remove(audio_path)
            logging.info(f"🧹 Deleted temp audio: {audio_path}")
        except Exception:
            pass

# ──────────────────────────────────────────────────────────────
# 🔊 FISH AUDIO TTS
# ──────────────────────────────────────────────────────────────

def handle_tts(query: str, thread_id: str) -> str:
    """Convert text to speech via Fish Audio (direct API) and send as voice note."""
    if not query:
        return "⚠️ Text likhein! Example: -tts Hello bhai kaise ho"

    logging.info(f"🔊 TTS generating [s2.1-pro-free]: {query[:60]}")
    tts_path = os.path.join(TEMP_AUDIO_DIR, f"tts_{int(time.time())}.mp3")

    try:
        response = requests.post(
            "https://api.fish.audio/v1/tts",
            headers={
                "Authorization": f"Bearer {FISH_API_KEY}",
                "Content-Type": "application/json",
                "model": "modEL_NAME"       # passed as header per Fish Audio docs
            },
            json={
                "text": query,
                "format": "mp3",
                "reference_id": "MODEL_REFERENCE_ID"   # voice: fish.audio/m/fc5451d24cc14e55bdc4fc76c00e76ae
            },
            timeout=60,
            stream=True
        )
        logging.info(f"📡 Fish Audio status: {response.status_code}")
        if response.status_code != 200:
            logging.error(f"❌ Fish Audio error: {response.text[:200]}")
            return f"❌ TTS failed: HTTP {response.status_code} — {response.text[:100]}"

        with open(tts_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        logging.info(f"✅ TTS saved: {tts_path} ({os.path.getsize(tts_path)} bytes)")

    except Exception as e:
        logging.error(f"❌ Fish Audio request error: {e}")
        return f"❌ TTS failed: {e}"

    try:
        client.direct_send_voice(tts_path, thread_ids=[int(thread_id)])
        logging.info(f"✅ TTS voice sent to thread {thread_id}")
        return None
    except Exception as e:
        logging.error(f"❌ TTS voice send failed: {e}")
        return f"❌ Could not send voice: {e}"
    finally:
        try:
            os.remove(tts_path)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# TEXT COMMAND HANDLERS
# ──────────────────────────────────────────────────────────────


def handle_urban(query: str) -> str:
    if not query:
        return "⚠️ Please specify a word! Example: -urban rizz"
    try:
        res = requests.get(f"https://api.urbandictionary.com/v0/define?term={requests.utils.quote(query)}").json()
        list_data = res.get('list', [])
        if list_data:
            definition = list_data[0].get('definition', '').replace('[', '').replace(']', '')
            example = list_data[0].get('example', '').replace('[', '').replace(']', '')
            return f"📚 *Urban Dict: {query}*\n\n📝 *Def:* {definition[:200]}...\n\n💡 *Ex:* _{example[:150]}_"
        else:
            return f"Could not find slang definition for '{query}'."
    except Exception:
        return "⚠️ Slang server timeout."

def handle_ai(query: str, thread_id: str = None, user_id=None, history: list = None) -> str:
    if not query:
        return "⚠️ Koi sawal poochein! Misal: -ai Aaj ka mosam kaisa hai?"

    # Mark this user as active in AI mode
    if thread_id and user_id:
        ai_active_users[(thread_id, str(user_id))] = time.time()

    # Build messages with history for context
    messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-10:])  # last 10 exchanges
    messages.append({"role": "user", "content": query})

    # Save user message to history
    if thread_id is not None:
        if thread_id not in convo_history:
            convo_history[thread_id] = []
        convo_history[thread_id].append({"role": "user", "content": query})

    result = call_ai_with_history(messages)
    if result:
        if thread_id is not None:
            convo_history[thread_id].append({"role": "assistant", "content": result})
            if len(convo_history[thread_id]) > 20:
                convo_history[thread_id] = convo_history[thread_id][-20:]
        return result
    return "⚠️ Kuch masla aa gaya, dobara try karein."

def handle_hack(query: str) -> str:
    if not query:
        return (
            "🔥 *-hack Command — Cybersecurity Info*\n\n"
            "Topics you can ask about:\n"
            "💥 DDoS attacks + protection\n"
            "🌐 IP tracking & anonymization\n"
            "🔓 Password cracking & brute force\n"
            "🚨 Phishing & social engineering\n"
            "📶 Wi-Fi hacking (WPA2, Evil Twin)\n"
            "🔥 SQL injection & XSS\n"
            "🔐 Firewalls, VPNs & anonymity\n"
            "🧰 Penetration testing basics\n\n"
            "Example: -hack how does ddos attack work"
        )
    logging.info(f"🔥 Hack query: {query}")
    result = call_ai(HACK_SYSTEM_PROMPT, query, max_tokens=500)
    if result:
        return result
    return "⚠️ Kuch masla aa gaya, dobara try karein."

# Keywords that indicate NSFW/adult content requests
NSFW_KEYWORDS = [
    "nude", "naked", "nsfw", "porn", "sex", "xxx", "adult", "explicit",
    "erotic", "hentai", "boobs", "breast", "genitals", "underwear strip",
    "undressed", "topless"
]

def handle_image(query: str, thread_id: str) -> str:
    if not query:
        return "⚠️ Provide a prompt for image! Example: -image cat playing guitar"

    # Block NSFW/adult content requests
    query_lower = query.lower()
    if any(word in query_lower for word in NSFW_KEYWORDS):
        return "❌ Sorry, I cannot generate that type of image. Please keep requests appropriate and respectful. 😊"

    logging.info(f"🎨 Generating image for prompt: '{query}'...")
    seed = random.randint(1, 99999)
    raw_img_path = os.path.join(TEMP_IMAGE_DIR, f"gen_{int(time.time())}_raw")
    temp_img_path = os.path.join(TEMP_IMAGE_DIR, f"gen_{int(time.time())}.jpg")
    download_success = False

    # Browser-like headers to avoid 403
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # ── Step 1: Download image from Pollinations ──
    image_url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(query)}?model=flux&width=1024&height=1024&nologo=true&seed={seed}"
    try:
        logging.info(f"🌐 Fetching from: {image_url}")
        response = requests.get(image_url, timeout=60, allow_redirects=True, headers=headers)
        content_type = response.headers.get('Content-Type', '')
        logging.info(f"📦 Response: status={response.status_code}, content-type={content_type}, size={len(response.content)} bytes")

        if response.status_code == 200 and 'image' in content_type:
            # Save raw bytes first
            with open(raw_img_path, "wb") as f:
                f.write(response.content)

            # ── Step 2: Re-encode as clean JPEG using PIL ──
            if PIL_AVAILABLE:
                try:
                    img = Image.open(raw_img_path).convert("RGB")
                    img.save(temp_img_path, format="JPEG", quality=90)
                    logging.info(f"✅ Re-encoded as JPEG: {temp_img_path} ({os.path.getsize(temp_img_path)} bytes)")
                    download_success = True
                except Exception as pil_err:
                    logging.error(f"PIL re-encode failed: {pil_err}")
                    # Fall back to raw file
                    shutil.copy(raw_img_path, temp_img_path)
                    if os.path.getsize(temp_img_path) > 1024:
                        download_success = True
            else:
                # No PIL — use raw file directly
                shutil.copy(raw_img_path, temp_img_path)
                if os.path.getsize(temp_img_path) > 1024:
                    download_success = True
                    logging.info(f"✅ Image saved (no PIL): {temp_img_path}")
        else:
            logging.error(f"❌ Pollinations bad response: status={response.status_code}, content-type={content_type}")
    except Exception as e:
        logging.error(f"❌ Pollinations download error: {e}")
    finally:
        # Clean up raw temp file
        try:
            if os.path.exists(raw_img_path):
                os.remove(raw_img_path)
        except Exception:
            pass

    # ── Step 3: Upload photo to DM ──
    if download_success and os.path.exists(temp_img_path):
        abs_path = os.path.abspath(temp_img_path)
        file_size = os.path.getsize(abs_path)
        logging.info(f"📤 Uploading photo: {abs_path} ({file_size} bytes) → thread {thread_id}")
        try:
            client.direct_send_photo(abs_path, thread_ids=[int(thread_id)])
            logging.info("✅ Photo uploaded successfully!")
            try:
                os.remove(abs_path)
            except Exception:
                pass
            return None  # Success — no text reply needed
        except Exception as upload_err:
            logging.error(f"❌ direct_send_photo failed: {type(upload_err).__name__}: {upload_err}")
    else:
        logging.error("❌ No valid image file to upload.")

    # ── Step 4: Clean up and fallback to link ──
    try:
        if os.path.exists(temp_img_path):
            os.remove(temp_img_path)
    except Exception:
        pass

    logging.warning("⚠️ Falling back to sending image URL as text.")
    fallback_msg = f"🎨 AI Image:\n{image_url}"
    try:
        client.direct_send(fallback_msg, thread_ids=[int(thread_id)])
    except Exception as e:
        logging.error(f"Fallback send failed: {e}")
    return None  # Return None so main loop doesn't double-send
# ──────────────────────────────────────────────────────────────
# MAIN POLLING LOOP
# ──────────────────────────────────────────────────────────────

def process_command(cmd: str, args: str, thread_id: str, sender_username: str) -> str:
    # Support both '!' and '-' prefixes by normalizing prefix to '-'
    if cmd.startswith('-'):
        cmd = '-' + cmd[1:]

    if cmd == "-menu":
        return (
            "🤖 Bot Commands\n\n"
            "📚 -urban + word\n"
            "🎵 -music + song name\n"
            "🎤 -vn + song name\n"
            "🎨 -generate + description\n"
            "💬 -ai + question\n"
            "🔥 -hack + topic\n"
            "🔊 -tts + text"
        )
    elif cmd == "-urban":
        return handle_urban(args)
    elif cmd == "-music":
        return handle_music_command(args, thread_id)
    elif cmd == "-vn":
        return handle_vn_command(args, thread_id)
    elif cmd == "-generate":
        return handle_image(args, thread_id)
    elif cmd == "-ai":
        return handle_ai(args, thread_id=thread_id, user_id=sender_username, history=convo_history.get(thread_id))
    elif cmd == "-hack":
        return handle_hack(args)
    elif cmd == "-tts":
        return handle_tts(args, thread_id)
    elif cmd == "-stop":
        user_key = (thread_id, str(sender_username))
        if user_key in ai_active_users:
            del ai_active_users[user_key]
        return "✅ AI session ended. Use -ai to start again."
    else:
        if args:
            return handle_ai(args)
        else:
            return "❌ Command not recognized. Type -menu for help."

def poll_dm():
    global last_seen_ids, sent_replies, processed_ids, ai_active_users, convo_history

    for thread_id in THREAD_IDS:
        try:
            # Retrieve messages using amount=10 and integer thread_id representation
            messages = client.direct_messages(int(thread_id), amount=10)
            
            # If the thread is empty at startup, initialize it so subsequent messages are not skipped
            if not messages:
                if last_seen_ids[thread_id] is None:
                    last_seen_ids[thread_id] = "initialized_empty"
                continue

            # If this is the first poll for this thread, mark all existing messages as processed (ignore old chats)
            if last_seen_ids[thread_id] is None:
                for msg in messages:
                    processed_ids.setdefault(thread_id, set()).add(msg.id)
                last_seen_ids[thread_id] = messages[0].id if messages else "initialized"
                logging.info(f"Skip History: Thread {thread_id} initialized. Ignored {len(messages)} old messages.")
                continue

            for msg in messages:
                msg_id = msg.id
                if msg_id in processed_ids.get(thread_id, set()):
                    continue
                processed_ids.setdefault(thread_id, set()).add(msg_id)

                if msg.text:
                    content = msg.text.strip()
                    if msg.user_id == client.user_id:
                        continue
                    if content in sent_replies:
                        continue

                    # ── Detect Instagram reply-to-bot ──
                    is_reply_to_bot = False
                    try:
                        msg_data = msg.dict() if hasattr(msg, 'dict') else {}
                        replied_raw = (
                            getattr(msg, 'replied_to_message', None) or
                            msg_data.get('replied_to_message') or
                            msg_data.get('reply') or
                            msg_data.get('replied_to')
                        )
                        if replied_raw is not None:
                            if isinstance(replied_raw, dict):
                                replied_uid = str(replied_raw.get('user_id', ''))
                            else:
                                replied_uid = str(getattr(replied_raw, 'user_id', ''))
                            is_reply_to_bot = (replied_uid == str(client.user_id))
                            logging.info(f"🔁 Reply: uid={replied_uid} bot={client.user_id} match={is_reply_to_bot}")
                    except Exception as re_err:
                        logging.error(f"Reply check error: {re_err}")

                    # ── Route: command or reply-to-bot ──
                    if content.startswith('-'):
                        parts = content.split(" ", 1)
                        cmd = parts[0].lower()
                        args = parts[1] if len(parts) > 1 else ""
                        logging.info(f"📥 [{thread_id}] Command from {msg.user_id}: {content}")
                        reply = process_command(cmd, args, thread_id, msg.user_id)
                    elif is_reply_to_bot:
                        logging.info(f"💬 [{thread_id}] Reply-to-bot from {msg.user_id}: {content}")
                        reply = handle_ai(content, thread_id=thread_id, user_id=msg.user_id, history=convo_history.get(thread_id))
                    else:
                        continue  # Ignore all other messages

                    if reply is not None:
                        try:
                            sent = client.direct_send(reply, thread_ids=[int(thread_id)])
                            try:
                                if hasattr(sent, 'id'):
                                    bot_message_ids.add(sent.id)
                            except Exception:
                                pass
                            sent_replies.append(reply)
                            if len(sent_replies) > 50:
                                sent_replies = sent_replies[-50:]
                            logging.info(f"\u2705 Replied: {reply[:60]}...")
                        except Exception as e:
                            logging.error(f"Failed to send reply: {e}")
                    else:
                        logging.info("\U0001f3b5 Media sent successfully (no text reply).")

            if messages:
                last_seen_ids[thread_id] = messages[-1].id

        except Exception as e:
            logging.error(f"Error polling thread {thread_id}: {e}")
            time.sleep(2)

def main():
    logging.info("🚀 Bot started. Polling DMs every 5 seconds.")
    while True:
        poll_dm()
        time.sleep(5)

if __name__ == "__main__":
    try:
        # Load cached session first if it exists to avoid checkpoint blocks
        if os.path.exists(SESSION_SETTINGS_PATH):
            try:
                client.load_settings(SESSION_SETTINGS_PATH)
                client.get_timeline_feed()
                print("✅ Logged in using cached session settings (session.json)")
                logged_in = True
            except Exception:
                logging.warning("Cached session expired/invalid. Logging in with Session ID...")
                client.set_settings({})
                logged_in = False
        else:
            logged_in = False
                
        # Perform login if not already authenticated via cache
        if not logged_in:
            session_success = False
            if SESSION_ID and SESSION_ID != "YOUR_SESSION_ID_HERE":
                try:
                    print("Trying to log in using SESSION_ID...")
                    client.login_by_sessionid(SESSION_ID)
                    print(f"✅ Logged in using session ID (user_id: {client.user_id})")
                    session_success = True
                except Exception as se:
                    logging.warning(f"Session ID login failed ({se}). Falling back to username/password...")
            
            if not session_success:
                print(f"Trying to log in as {IG_USERNAME} using password...")
                client.login(IG_USERNAME, IG_PASSWORD)
                print(f"✅ Logged in as {IG_USERNAME} (user_id: {client.user_id})")

            client.dump_settings(SESSION_SETTINGS_PATH)
            print("💾 Session settings saved to session.json")

        main()
    except KeyboardInterrupt:
        logging.info("🛑 Bot stopped by user.")
    finally:
        try:
            shutil.rmtree(TEMP_AUDIO_DIR)
            logging.info(f"🧹 Cleaned up temp folder: {TEMP_AUDIO_DIR}")
        except Exception:
            pass