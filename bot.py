import os
import re
import io
import html
import logging
import asyncio
import tempfile
import urllib.request
import urllib.parse
import json
import hashlib
import difflib
import time
import threading
import base64
import contextlib
from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, APIC, TIT2, TPE1, TALB, TPE2, TDRC, USLT, TCOM, TEXT, TPUB, ID3NoHeaderError
)
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InlineQueryResultAudio, InputMediaAudio
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler,
    CallbackQueryHandler, CommandHandler, filters,
    InlineQueryHandler, ChosenInlineResultHandler
)
from ytmusicapi import YTMusic
import yt_dlp
import concurrent.futures

try:
    from cachetools import TTLCache
except ImportError:
    raise ImportError("cachetools is required. Run: pip install cachetools")

try:
    from peewee import Model, IntegerField, CharField, SqliteDatabase
except ImportError:
    raise ImportError("peewee is required. Run: pip install peewee")

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv("TOKEN")
DUMP_CHAT_ID = os.getenv("DUMP_CHAT_ID")
GENIUS_ACCESS_TOKEN = os.getenv("GENIUS_ACCESS_TOKEN") or os.getenv("GENIUS_API_KEY")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID") or None
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET") or None
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY") or None
LASTFM_ICON_EMOJI_ID = os.getenv("LASTFM_ICON_EMOJI_ID", "5298951425114520052")
YANDEX_ICON_EMOJI_ID = os.getenv("YANDEX_ICON_EMOJI_ID", "5346080651009371683")
# bgutil PO token provider (brainicism/bgutil-ytdlp-pot-provider Docker container).
BGUTIL_URL = (os.getenv("BGUTIL_URL") or "http://127.0.0.1:4416").rstrip("/")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "musicbot_temp")
COOKIES_DIR = os.path.join(BASE_DIR, "cookies")

# Tracks longer than this are refused before download (Telegram bots can upload max 50 MB).
MAX_DURATION_SECONDS = 600

# "m4a":  keep YouTube's original AAC audio without re-encoding (default). Fastest, smallest
#         upload, no quality loss; full tags and cover art as MP4 atoms.
# "mp3":  re-encode everything to 192 kbps MP3 (~2 s of CPU per song, ~1/3 bigger files).
# "opus": keep YouTube's original Opus audio without re-encoding. Faster, but Telegram turns
#         .opus uploads into voice messages, which fail with "Voice_messages_forbidden" for
#         users who restrict voice messages. Do not enable unless that is acceptable.
AUDIO_FORMAT = (os.getenv("AUDIO_FORMAT") or "m4a").strip().lower()
if AUDIO_FORMAT not in ("m4a", "opus", "mp3"):
    print(f"Unknown AUDIO_FORMAT '{AUDIO_FORMAT}', falling back to m4a.")
    AUDIO_FORMAT = "m4a"
_AUDIO_EXTENSIONS = ('.opus', '.mp3', '.m4a', '.ogg')


ALLOWED_USERS = set()
try:
    user_list = os.getenv("ALLOWED_USERS", "")
    ALLOWED_USERS = {int(x.strip()) for x in user_list.split(",") if x.strip()}
except ValueError:
    print("Error parsing ALLOWED_USERS. Check your .env file.")

if not ALLOWED_USERS:
    print("WARNING: ALLOWED_USERS is empty. No one will be able to use the bot!")

if not DUMP_CHAT_ID:
    print("WARNING: DUMP_CHAT_ID not set. Inline mode audio caching will not work.")
else:
    try:
        DUMP_CHAT_ID = int(DUMP_CHAT_ID)
    except ValueError:
        print("Error: DUMP_CHAT_ID must be a valid integer (chat ID).")
        DUMP_CHAT_ID = None

CACHE_TTL = 600
SEARCH_CACHE: TTLCache = TTLCache(maxsize=500, ttl=CACHE_TTL)
# Inline results wait here until the user picks one. Bounded so it cannot grow forever.
INLINE_SEARCH_CACHE: TTLCache = TTLCache(maxsize=2000, ttl=3600)
# Results of search_hybrid by query, so repeated / inline-keystroke searches are instant.
HYBRID_SEARCH_CACHE: TTLCache = TTLCache(maxsize=256, ttl=300)
_latest_inline_query: dict = {}

USER_SETTINGS_FILE = os.getenv("USER_SETTINGS_FILE") or os.path.join(BASE_DIR, "users_data.json")
AWAITING_STATS_FM_USERNAME = set()
AWAITING_LASTFM_USERNAME = set()
AWAITING_COOKIES_INPUT = set()

_settings_cache: dict | None = None

# --- DATABASE FOR AUDIO FILE CACHING ---
DB_PATH = os.getenv("AUDIO_CACHE_DB") or os.path.join(BASE_DIR, "audio_cache.db")
_cached_audio_db = None
_cached_audio_model = None

def init_audio_cache():
    global _cached_audio_db, _cached_audio_model
    try:
        db = SqliteDatabase(DB_PATH, pragmas={'journal_mode': 'wal', 'cache_size': -1024 * 64, 'busy_timeout': 5000})

        class CachedAudio(Model):
            video_id = CharField(unique=True, index=True)
            tg_file_id = CharField(unique=True)
            title = CharField(null=True)
            performer = CharField(null=True)
            duration = IntegerField(null=True)

            class Meta:
                database = db

        db.connect()
        db.create_tables([CachedAudio], safe=True)
        _cached_audio_db = db
        _cached_audio_model = CachedAudio
        logging.info(f"Audio cache database initialized: {DB_PATH}")
    except Exception as e:
        logging.error(f"Failed to initialize audio cache database: {e}")

async def get_cached_audio(video_id: str):
    if _cached_audio_model is None:
        return None
    def _get():
        try:
            return _cached_audio_model.get_or_none(_cached_audio_model.video_id == video_id)
        except Exception as e:
            logging.error(f"Error getting cached audio: {e}")
            return None
    return await asyncio.to_thread(_get)

async def save_cached_audio(video_id: str, tg_file_id: str, title: str = None, performer: str = None, duration: int = None):
    if _cached_audio_model is None:
        return None
    def _save():
        try:
            obj, created = _cached_audio_model.get_or_create(
                video_id=video_id,
                defaults={'tg_file_id': tg_file_id, 'title': title, 'performer': performer, 'duration': duration}
            )
            if not created:
                obj.tg_file_id = tg_file_id
                if title: obj.title = title
                if performer: obj.performer = performer
                if duration: obj.duration = duration
                obj.save()
            return obj
        except Exception as e:
            logging.error(f"Error saving cached audio: {e}")
            return None
    return await asyncio.to_thread(_save)


# --- SETTINGS ---

_settings_lock = threading.Lock()


def load_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        with _settings_lock:
            if _settings_cache is None:
                data = {}
                if os.path.exists(USER_SETTINGS_FILE):
                    try:
                        with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        # Keep the broken file for inspection instead of crashing every handler.
                        backup = f"{USER_SETTINGS_FILE}.corrupt-{int(time.time())}"
                        logging.error(f"Settings file is corrupt ({e}); moved to {backup}, starting empty.")
                        os.replace(USER_SETTINGS_FILE, backup)
                        data = {}
                _settings_cache = data
    return _settings_cache


def save_settings(data: dict):
    """Write settings atomically: a crash or a parallel write can never leave half a file."""
    global _settings_cache
    _settings_cache = data
    with _settings_lock:
        payload = json.dumps(data, indent=4, ensure_ascii=False)
        tmp_path = f"{USER_SETTINGS_FILE}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, USER_SETTINGS_FILE)


def get_user_setting(user_id, key, default=None):
    return load_settings().get(str(user_id), {}).get(key, default)

def set_user_setting(user_id, key, value):
    data = load_settings()
    data.setdefault(str(user_id), {})[key] = value
    save_settings(data)


# --- PLATFORM DETECTION ---
YTDLP_DIRECT_PATTERNS = [
    re.compile(r'youtube\.com/watch', re.IGNORECASE),
    re.compile(r'youtu\.be/', re.IGNORECASE),
    re.compile(r'music\.youtube\.com', re.IGNORECASE),
    re.compile(r'soundcloud\.com', re.IGNORECASE),
    re.compile(r'vk\.com/audio', re.IGNORECASE),
    re.compile(r'vk\.com/music', re.IGNORECASE),
]

SPOTIFY_PATTERNS = [
    re.compile(r'open\.spotify\.com', re.IGNORECASE),
    re.compile(r'spotify\.link', re.IGNORECASE),
]

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ytmusic = YTMusic()
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(COOKIES_DIR, exist_ok=True)
init_audio_cache()


# --- HELPERS: URL DETECTION ---
def extract_url(text: str) -> str | None:
    match = re.search(r'https?://[^\s]+', text)
    if match:
        url = match.group(0)
        # Strip trailing punctuation commonly appended to URLs in text
        url = url.rstrip('.,;:!?()[]{}<>\"\'')
        return url
    return None

def is_url(text: str) -> bool:
    t = text.strip()
    return t.startswith("http://") or t.startswith("https://")

def get_url_type(url: str) -> str:
    for pattern in YTDLP_DIRECT_PATTERNS:
        if pattern.search(url):
            return 'direct'
    for pattern in SPOTIFY_PATTERNS:
        if pattern.search(url):
            return 'spotify'
    return 'direct'


# --- HELPER: BLOCKING HTTP GET -> JSON ---
def _http_get_json(url: str, headers: dict = None, timeout: int = 10) -> dict | None:
    req_headers = {"User-Agent": "MusicBot/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# --- HELPER: NATIVE SPOTIFY TRACK RESOLVER ---
def resolve_spotify_track(url: str) -> dict | None:
    """
    Directly extracts track metadata from Spotify (artist, title, duration)
    without requiring third-party API keys or services.
    Supports open.spotify.com and redirecting spotify.link URLs.
    """
    # 1. Resolve redirect if shortened link
    if 'spotify.link' in url:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            with urllib.request.urlopen(req, timeout=8) as resp:
                url = resp.geturl()
        except Exception as e:
            logging.debug(f"Failed to follow spotify.link redirect: {e}")

    # 2. Extract track ID
    match = re.search(r'/track/([a-zA-Z0-9]+)', url)
    if not match:
        return None
    track_id = match.group(1)
    spotify_url = f"https://open.spotify.com/track/{track_id}"

    # 3. Try embed endpoint
    embed_url = f"https://open.spotify.com/embed/track/{track_id}"
    try:
        req = urllib.request.Request(embed_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            m = re.search(r'id=[\x22\x27]__NEXT_DATA__[\x22\x27][^>]*>([^<]+)<', html)
            if m:
                data = json.loads(m.group(1))
                entity = data.get('props', {}).get('pageProps', {}).get('state', {}).get('data', {}).get('entity', {})
                title = entity.get('name') or entity.get('title')
                artists = [a.get('name') for a in entity.get('artists', []) if a.get('name')]
                artist = ', '.join(artists) if artists else None
                images = [i for i in entity.get('visualIdentity', {}).get('image', []) if i.get('url')]
                # Image list order is not guaranteed — pick the largest one
                best_img = max(images, key=lambda i: i.get('maxWidth') or i.get('width') or 0) if images else None
                cover_url = best_img.get('url') if best_img else None
                duration_ms = entity.get('duration')
                if title:
                    return {
                        'title': title,
                        'artist': artist,
                        'duration': duration_ms // 1000 if duration_ms else None,
                        'track_id': track_id,
                        'spotify_url': spotify_url,
                        'cover_url': cover_url,
                    }
    except Exception as e:
        logging.debug(f"Spotify embed metadata extraction failed: {e}")

    # 4. Fallback: oEmbed
    try:
        oembed_url = f"https://open.spotify.com/oembed?url={urllib.parse.quote(spotify_url)}"
        req = urllib.request.Request(oembed_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
            title = data.get('title')
            cover_url = data.get('thumbnail_url')
            if title:
                return {
                    'title': title,
                    'artist': None,
                    'duration': None,
                    'track_id': track_id,
                    'spotify_url': spotify_url,
                    'cover_url': cover_url,
                }
    except Exception as e:
        logging.debug(f"Spotify oEmbed extraction failed: {e}")

    # 5. Fallback: Open Graph meta tags
    try:
        req = urllib.request.Request(spotify_url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(65536).decode('utf-8', errors='ignore')
            og_title = re.search(r'<meta\s+property=[\x22\x27]og:title[\x22\x27]\s+content=[\x22\x27]([^\x22\x27]+)[\x22\x27]', html, re.I)
            og_desc = re.search(r'<meta\s+property=[\x22\x27]og:description[\x22\x27]\s+content=[\x22\x27]([^\x22\x27]+)[\x22\x27]', html, re.I)
            og_img = re.search(r'<meta\s+property=[\x22\x27]og:image[\x22\x27]\s+content=[\x22\x27]([^\x22\x27]+)[\x22\x27]', html, re.I)
            if og_title:
                title = og_title.group(1).strip()
                artist = None
                if og_desc:
                    # Description is usually "Artist · Song · Year"
                    parts = og_desc.group(1).split(' · ')
                    if parts:
                        artist = parts[0].strip()
                cover_url = og_img.group(1).strip() if og_img else None
                return {
                    'title': title,
                    'artist': artist,
                    'duration': None,
                    'track_id': track_id,
                    'spotify_url': spotify_url,
                    'cover_url': cover_url,
                }
    except Exception as e:
        logging.debug(f"Spotify Open Graph extraction failed: {e}")

    return None


# --- HELPER: CLEAN TITLE & LYRICS ---
def clean_title_for_search(title: str) -> str:
    if not title:
        return ""
    # Strip status / inline prefixes like (stats.fm):, (last.fm):, (recent):, Currently playing:, etc.
    t = re.sub(r'^\s*[\(\[]?\s*(?:stats\.fm|last\.fm|recent|now playing|currently playing|last played)\s*[\)\]]?\s*[:\-–—]?\s*', '', title, flags=re.I)
    t = re.sub(r'[\(\[\{].*?(?:official|video|audio|remaster|visualizer|lyric|clip|full\s*ver|4k|hd|hq).*?[\)\]\}]', '', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


_MUSIC_DESCRIPTORS = {
    'feat', 'ft', 'featuring', 'with', 'prod', 'remix', 'mix', 'version', 'ver',
    'live', 'acoustic', 'instrumental', 'orchestral', 'edit', 'deluxe', 'bonus',
    'slowed', 'reverb', 'speed', 'sped', 'part', 'pt', 'ost', 'soundtrack', 'theme',
    'intro', 'outro', 'radio', 'club', 'extended', 'cover', 'original', 'remaster', 'remastered'
}

_NON_LATIN_PATTERN = re.compile(
    r'[\u0400-\u04FF\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF\u0600-\u06FF\u0370-\u03FF]'
)


def clean_genius_title(title: str) -> str:
    """
    Remove English/Latin title translations and romanizations appended in brackets by Genius,
    e.g. 'Положение (Position)' -> 'Положение'
         'ты че обиделась (are you offended)' -> 'ты че обиделась'
         'Город под подошвой (City Under the Sole)' -> 'Город под подошвой'
    Preserves legitimate music descriptors like '(feat. ...)', '(Remix)', '(Live)', etc.
    """
    if not title:
        return ""
    if not _NON_LATIN_PATTERN.search(title):
        return title.strip()

    def _replacer(match):
        content = match.group(1).strip()
        words = re.findall(r'[a-zA-Z]+', content.lower())
        # If any word is a legitimate musical descriptor, keep it
        if any(w in _MUSIC_DESCRIPTORS for w in words):
            return match.group(0)
        # If the bracketed part contains non-Latin characters (e.g. (feat. Скриптонит)), keep it
        if _NON_LATIN_PATTERN.search(content):
            return match.group(0)
        # Otherwise, the bracketed part is a Latin translation/romanization -> remove
        return ""

    cleaned = re.sub(r'[\(\[]\s*([^\(\)\[\]]+?)\s*[\)\]]', _replacer, title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def clean_lyrics_text(text: str) -> str:
    if not text:
        return ""
    lines = text.strip().splitlines()
    if lines:
        first = lines[0].strip().lstrip('\u200b')
        if (
            re.search(r'текст\s+песни', first, re.IGNORECASE)
            or re.search(r'(?:lyrics|가사|歌詞)\s*\]?$', first, re.IGNORECASE)
            or re.search(r'^\[.*?lyrics.*?\]$', first, re.IGNORECASE)
        ):
            lines = lines[1:]
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r'\d*Embed$', '', cleaned).strip()
    cleaned = re.sub(r'You might also like.*$', '', cleaned, flags=re.MULTILINE).strip()
    return cleaned


def is_romanized_or_translation(hit_title: str, hit_artist: str, primary_artist: str) -> bool:
    """Detect if a Genius song result is a romanization, translation, or community junk."""
    text_to_check = f"{hit_title} {hit_artist} {primary_artist}".lower()
    if any(k in text_to_check for k in ['romaniz', 'романизац']):
        return True
    if any(k in text_to_check for k in ['translat', 'перевод', 'traducc', 'traduct', 'übersetz', 'çeviri']):
        return True
    if any(k in primary_artist.lower() for k in ['genius romanization', 'genius translation', 'genius english', 'genius russian', 'genius ']):
        return True
    if any(k in text_to_check for k in ['tracklist', 'discograph', 'дискографи', 'liner notes', 'album review', 'hottest tracks']):
        return True
    return False


# --- HELPER: SMALL THREAD-SAFE TTL CACHE (lookups run in worker threads) ---
_CACHE_MISS = object()


class _LookupCache:
    """Remembers successful lookups for a long time and "nothing found" for a short time."""

    def __init__(self, maxsize: int = 500, hit_ttl: int = 6 * 3600, miss_ttl: int = 600):
        self._hits = TTLCache(maxsize=maxsize, ttl=hit_ttl)
        self._misses = TTLCache(maxsize=maxsize, ttl=miss_ttl)
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._hits.get(key, _CACHE_MISS)
            if value is _CACHE_MISS:
                value = self._misses.get(key, _CACHE_MISS)
            return value

    def set(self, key, value, found: bool):
        with self._lock:
            if found:
                self._hits[key] = value
                self._misses.pop(key, None)
            else:
                self._misses[key] = value

    def clear(self):
        with self._lock:
            self._hits.clear()
            self._misses.clear()


_lrclib_cache = _LookupCache()
_genius_meta_cache = _LookupCache()
_genius_lyrics_cache = _LookupCache()
# Lets the Genius page scrape and the LRCLIB request run side by side.
_LYRICS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="lyrics")
# Metadata lookups started from yt-dlp worker threads (no event loop there).
_META_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="meta")


def clear_lookup_caches():
    for cache in (_lrclib_cache, _genius_meta_cache, _genius_lyrics_cache):
        cache.clear()


# --- HELPER: LRCLIB LYRICS API ---
def fetch_lrclib_lyrics(artist: str = None, track: str = None) -> str | None:
    """Fetch lyrics from LRCLIB as a free fallback (no API key required). Cached."""
    if not track:
        return None
    clean_track = clean_title_for_search(track)
    clean_artist = artist.strip() if artist else ""
    key = (clean_artist.lower(), clean_track.lower())
    cached = _lrclib_cache.get(key)
    if cached is not _CACHE_MISS:
        return cached
    try:
        lyrics = None
        if clean_artist:
            url = f"https://lrclib.net/api/get?artist_name={urllib.parse.quote(clean_artist)}&track_name={urllib.parse.quote(clean_track)}"
            data = _http_get_json(url, timeout=5)
            if data and data.get("plainLyrics"):
                lyrics = data["plainLyrics"].strip()

        if not lyrics:
            # Try search endpoint if exact get fails or artist not specified
            query = f"{clean_artist} {clean_track}".strip()
            search_url = f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}"
            results = _http_get_json(search_url, timeout=5)
            if results and isinstance(results, list):
                for item in results:
                    if item.get("plainLyrics"):
                        lyrics = item["plainLyrics"].strip()
                        break
        _lrclib_cache.set(key, lyrics, found=bool(lyrics))
        return lyrics
    except Exception as e:
        # Network trouble: do not cache, so the next request tries again.
        logging.debug(f"LRCLIB lyrics fetch failed: {e}")
    return None


# --- HELPER: GENIUS CLIENT & API ---
_genius_client = None

def get_genius_client():
    global _genius_client
    if _genius_client is None and GENIUS_ACCESS_TOKEN:
        try:
            import lyricsgenius
            _genius_client = lyricsgenius.Genius(
                GENIUS_ACCESS_TOKEN,
                response_format='plain',
                timeout=8,
                sleep_time=0.1,
                skip_non_songs=True,
                remove_section_headers=False,
            )
            _genius_client.verbose = False
        except Exception as e:
            logging.error(f"Failed to initialize lyricsgenius: {e}")
            _genius_client = None
    return _genius_client


# --- HELPER: GENIUS API (METADATA, COVERS, APPLE MUSIC LINK, LYRICS) ---
def _is_genius_placeholder(url: str | None) -> bool:
    """Genius returns a generic placeholder image when a song has no artwork."""
    if not url:
        return True
    lower = url.lower()
    return 'default_cover' in lower or 'default_avatar' in lower or 'assets.genius.com/images/' in lower


def _empty_genius_result() -> dict:
    return {
        'matched': False,
        'genius_id': None,
        'title': None,
        'artist': None,
        'cover_url': None,
        'apple_music_id': None,
        'apple_music_url': None,
        'song_page_url': None,
        'lyrics': None,
        'embed_lyrics': None,
        'genius_lyrics': None,
        'lrclib_lyrics': None,
        'album': None,
        'release_date': None,
        'composer': None,
        'lyricist': None,
    }


def _fetch_genius_meta(clean_a: str, clean_t: str) -> tuple[dict, bool]:
    """
    Search Genius and return (metadata, definitive). No lyrics here.
    definitive=False means the request itself failed, so the result must not be cached.
    """
    res_dict = _empty_genius_result()
    try:
        query = f"{clean_t} {clean_a}".strip() if clean_a else clean_t
        search_url = f"https://api.genius.com/search?q={urllib.parse.quote(query)}"
        search_data = _http_get_json(
            search_url,
            headers={'Authorization': f"Bearer {GENIUS_ACCESS_TOKEN}"},
            timeout=8
        )
    except Exception as e:
        logging.error(f"Genius search failed: {e}")
        return res_dict, False

    hits = (search_data or {}).get('response', {}).get('hits', [])
    clean_art_lower = clean_a.lower()
    clean_tit_lower = clean_t.lower()

    best_hit = None
    best_score = -1.0

    for hit in hits:
        if hit.get('type') != 'song':
            continue
        r = hit.get('result', {})
        hit_title = r.get('title', '')
        hit_artist = r.get('artist_names', '')
        primary_artist = r.get('primary_artist', {}).get('name', '')

        # Strictly skip translations, romanizations, and non-song community entries
        if is_romanized_or_translation(hit_title, hit_artist, primary_artist):
            continue

        hit_title_lower = hit_title.lower()
        hit_artist_lower = hit_artist.lower()
        primary_artist_lower = primary_artist.lower()

        title_ratio = difflib.SequenceMatcher(None, clean_tit_lower, hit_title_lower).ratio()
        if clean_tit_lower in hit_title_lower or hit_title_lower in clean_tit_lower:
            title_ratio = max(title_ratio, 0.85)

        artist_ratio = 0.0
        if clean_art_lower:
            if clean_art_lower in hit_artist_lower or clean_art_lower in primary_artist_lower or primary_artist_lower in clean_art_lower:
                artist_ratio = 1.0
            else:
                artist_ratio = difflib.SequenceMatcher(None, clean_art_lower, primary_artist_lower).ratio()
        else:
            artist_ratio = 0.5

        score = (title_ratio * 0.6) + (artist_ratio * 0.4)
        if score > best_score:
            best_score = score
            best_hit = r

    if not best_hit or best_score < 0.5:
        return res_dict, True

    song_id = best_hit.get('id')
    song_url = f"https://api.genius.com/songs/{song_id}"
    song = best_hit
    try:
        full_data = _http_get_json(
            song_url,
            headers={'Authorization': f"Bearer {GENIUS_ACCESS_TOKEN}"},
            timeout=8
        )
        if full_data and full_data.get('response', {}).get('song'):
            song = full_data['response']['song']
    except Exception as e:
        logging.warning(f"Genius song details fetch failed for {song_id}: {e}")

    res_dict['matched'] = True
    raw_title = song.get('title') or best_hit.get('title')
    res_dict['title'] = clean_genius_title(raw_title) if raw_title else None
    raw_artist = song.get('artist_names') or best_hit.get('artist_names')
    res_dict['artist'] = clean_genius_title(raw_artist) if raw_artist else None
    # High resolution cover art. Skip Genius placeholder images so that the
    # provider/YouTube artwork fallbacks can kick in. header_image_url is
    # intentionally not used: it is often an artist banner, not cover art.
    res_dict['cover_url'] = next(
        (u for u in (
            song.get('song_art_image_url'),
            (song.get('album') or {}).get('cover_art_url'),
            best_hit.get('song_art_image_url'),
        ) if not _is_genius_placeholder(u)),
        None
    )
    amid = song.get('apple_music_id')
    res_dict['apple_music_id'] = str(amid) if amid else None
    if amid:
        res_dict['apple_music_url'] = f"https://music.apple.com/song/{amid}"

    album_obj = song.get('album')
    if album_obj and isinstance(album_obj, dict):
        res_dict['album'] = album_obj.get('name')
    res_dict['release_date'] = song.get('release_date')

    writers = [w.get('name') for w in song.get('writer_artists', []) if w.get('name')]
    if writers:
        res_dict['lyricist'] = ', '.join(writers)
    producers = [p.get('name') for p in song.get('producer_artists', []) if p.get('name')]
    if producers:
        res_dict['composer'] = ', '.join(producers)

    page_url = song.get('url') or best_hit.get('url')
    res_dict['song_page_url'] = page_url

    res_dict['genius_id'] = song_id
    return res_dict, True


def _fetch_genius_lyrics(song_id) -> str | None:
    """Scrape lyrics from the Genius song page (slow). Cached."""
    if not song_id:
        return None
    cached = _genius_lyrics_cache.get(song_id)
    if cached is not _CACHE_MISS:
        return cached
    client = get_genius_client()
    if not client:
        return None
    try:
        raw_lyrics = client.lyrics(song_id=song_id)
        lyrics = (clean_lyrics_text(raw_lyrics) if raw_lyrics else None) or None
        _genius_lyrics_cache.set(song_id, lyrics, found=bool(lyrics))
        return lyrics
    except Exception as e:
        logging.debug(f"Genius lyrics scrape failed for {song_id}: {e}")
        return None


def fetch_genius_data(artist: str = None, track: str = None, fetch_lyrics: bool = True,
                      genius_lyrics: bool = True) -> dict:
    """
    Search Genius API, match track and artist accurately (strictly original, not romanized),
    and fetch high-resolution cover art, Apple Music ID, album, release date, contributors,
    and lyrics. Results are cached, so repeated calls for the same track cost nothing.

    fetch_lyrics=False   -- metadata/cover only, no lyrics requests at all.
    genius_lyrics=False  -- lyrics are only needed for embedding into the file: use LRCLIB
                            and scrape the (slow) Genius page only if LRCLIB has nothing.
    """
    if not track:
        return _empty_genius_result()

    clean_t = clean_title_for_search(track)
    clean_a = (artist or '').strip()

    if GENIUS_ACCESS_TOKEN:
        key = (clean_a.lower(), clean_t.lower())
        cached = _genius_meta_cache.get(key)
        if cached is _CACHE_MISS:
            meta, definitive = _fetch_genius_meta(clean_a, clean_t)
            if definitive:
                _genius_meta_cache.set(key, meta, found=bool(meta.get('matched')))
        else:
            meta = cached
        res_dict = dict(meta)
    else:
        res_dict = _empty_genius_result()

    if not fetch_lyrics:
        return res_dict

    song_id = res_dict.get('genius_id') if res_dict.get('matched') else None
    lrclib_artist = res_dict.get('artist') or clean_a
    lrclib_track = res_dict.get('title') or clean_t

    def _lrclib():
        found = fetch_lrclib_lyrics(lrclib_artist, lrclib_track)
        if not found and (clean_a != lrclib_artist or clean_t != lrclib_track):
            found = fetch_lrclib_lyrics(clean_a, clean_t)
        return found

    genius_text = None
    if song_id and genius_lyrics:
        # Both sources are wanted: fetch them at the same time.
        genius_future = _LYRICS_POOL.submit(_fetch_genius_lyrics, song_id)
        lrclib_lyrics = _lrclib()
        try:
            genius_text = genius_future.result(timeout=30)
        except Exception as e:
            logging.debug(f"Genius lyrics future failed: {e}")
    else:
        lrclib_lyrics = _lrclib()
        if not lrclib_lyrics and song_id:
            genius_text = _fetch_genius_lyrics(song_id)

    res_dict['genius_lyrics'] = genius_text
    res_dict['lrclib_lyrics'] = lrclib_lyrics
    # For chat: prefer Genius, fallback to LRCLIB if Genius fetch failed
    res_dict['lyrics'] = genius_text or lrclib_lyrics
    # For embedding: always use LRCLIB, fallback to Genius if LRCLIB has no lyrics
    res_dict['embed_lyrics'] = lrclib_lyrics or genius_text
    return res_dict



def _fetch_metadata_bundle(
    video_id: str,
    artist: str = None,
    track: str = None,
    original_url: str = None,
    include_lyrics: bool = False,
) -> dict:
    """
    Concurrent metadata pipeline:
    Fetches Genius metadata and lyrics (Genius preferred for chat, LRCLIB always for embedding).
    """
    genius_data = {}
    if track or artist:
        try:
            genius_data = fetch_genius_data(artist=artist, track=track, genius_lyrics=include_lyrics)
        except Exception as e:
            logging.warning(f"Error fetching Genius data: {e}")

    chat_lyrics = genius_data.get('lyrics')
    embed_lyrics = genius_data.get('embed_lyrics')

    # Ensure embed_lyrics is fetched from LRCLIB if not already obtained
    if not embed_lyrics and (artist or track):
        try:
            embed_lyrics = fetch_lrclib_lyrics(artist, track)
        except Exception as e:
            logging.debug(f"LRCLIB embed fetch failed: {e}")

    if not chat_lyrics:
        chat_lyrics = embed_lyrics

    if not embed_lyrics and chat_lyrics:
        embed_lyrics = chat_lyrics

    cover_url = genius_data.get('cover_url')

    return {
        'genius': genius_data,
        'lyrics': chat_lyrics,
        'embed_lyrics': embed_lyrics,
        'cover_url': cover_url,
    }


async def _send_lyrics_message(bot, chat_id: int, artist: str, title: str, lyrics: str):
    """Send lyrics as a separate message formatted with a collapsed/expandable blockquote."""
    if not lyrics:
        return
    clean_art = clean_genius_title(artist) or artist or "Unknown Artist"
    clean_tit = clean_genius_title(title) or title or "Unknown Track"
    escaped_art = html.escape(clean_art)
    escaped_tit = html.escape(clean_tit)
    header = f"<b>{escaped_art} — {escaped_tit}</b>\n\n"
    escaped_lyrics = html.escape(lyrics.strip())
    full_text = f"{header}<blockquote expandable>{escaped_lyrics}</blockquote>"

    if len(full_text) <= 4096:
        try:
            await bot.send_message(chat_id=chat_id, text=full_text, parse_mode='HTML')
            return
        except Exception as e:
            logging.warning(f"Failed to send lyrics as expandable HTML: {e}")
            try:
                await bot.send_message(chat_id=chat_id, text=f"<b>{escaped_art} — {escaped_tit}</b>\n\n{escaped_lyrics}", parse_mode='HTML')
                return
            except Exception:
                await bot.send_message(chat_id=chat_id, text=f"{clean_art} — {clean_tit}\n\n{lyrics}")
                return

    # Split into chunks of max 3900 chars respecting line boundaries
    lines = escaped_lyrics.splitlines(keepends=True)
    chunks = []
    current_lines = []
    current_len = len(header) + len("<blockquote expandable></blockquote>")

    for line in lines:
        if current_len + len(line) > 3900 and current_lines:
            chunks.append("".join(current_lines))
            current_lines = [line]
            current_len = len("<blockquote expandable></blockquote>") + len(line)
        else:
            current_lines.append(line)
            current_len += len(line)
    if current_lines:
        chunks.append("".join(current_lines))

    for idx, chunk in enumerate(chunks):
        if idx == 0:
            msg_text = f"{header}<blockquote expandable>{chunk}</blockquote>"
        else:
            msg_text = f"<blockquote expandable>{chunk}</blockquote>"
        try:
            await bot.send_message(chat_id=chat_id, text=msg_text, parse_mode='HTML')
        except Exception as e:
            logging.warning(f"Failed to send chunk {idx} as expandable HTML: {e}")
            await bot.send_message(chat_id=chat_id, text=chunk)



# --- HELPER: STREAMING SERVICE LINKS (SPOTIFY & APPLE MUSIC) ---
_spotify_token_lock = threading.Lock()
_spotify_token_data = {"token": None, "expires_at": 0.0}
_spotify_track_cache = TTLCache(maxsize=500, ttl=86400)
_apple_track_cache = TTLCache(maxsize=500, ttl=86400)


def get_spotify_access_token() -> str | None:
    """Fetch Spotify OAuth access token using client credentials flow, cached for 1 hour."""
    now = time.time()
    with _spotify_token_lock:
        if _spotify_token_data["token"] and now < (_spotify_token_data["expires_at"] - 60):
            return _spotify_token_data["token"]

        client_id = SPOTIFY_CLIENT_ID
        client_secret = SPOTIFY_CLIENT_SECRET
        if not client_id or not client_secret:
            return None

        try:
            token_url = "https://accounts.spotify.com/api/token"
            data = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            }).encode("utf-8")
            req = urllib.request.Request(
                token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                token = res_data.get("access_token")
                expires_in = res_data.get("expires_in", 3600)
                _spotify_token_data["token"] = token
                _spotify_token_data["expires_at"] = now + expires_in
                return token
        except Exception as e:
            logging.error(f"Error fetching Spotify access token: {e}")
            return None


def search_spotify_track(artist: str = None, track: str = None) -> str | None:
    """
    Search Spotify for a track and return its web URL.
    Uses 'artist:<artist> track:<track>' search format first,
    falling back to clean title/artist query.
    """
    clean_t = clean_title_for_search(track) if track else ""
    clean_a = clean_uploader(artist) if artist else ""
    if clean_a:
        clean_t = re.sub(rf'^\s*{re.escape(clean_a)}\s*[-–—:]\s*', '', clean_t, flags=re.I).strip()

    cache_key = (clean_a.lower(), clean_t.lower())
    if cache_key in _spotify_track_cache:
        return _spotify_track_cache[cache_key]

    token = get_spotify_access_token()
    if not token:
        return None

    queries = []
    if clean_a and clean_t:
        queries.append(f"artist:{clean_a} track:{clean_t}")
        queries.append(f"{clean_a} {clean_t}".strip())
    elif clean_t:
        queries.append(f"track:{clean_t}")
        queries.append(clean_t)
    elif clean_a:
        queries.append(f"artist:{clean_a}")

    for q in queries:
        try:
            params = urllib.parse.urlencode({
                "q": q,
                "type": "track",
                "limit": 1,
            })
            search_url = f"https://api.spotify.com/v1/search?{params}"
            req = urllib.request.Request(
                search_url,
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                res_json = json.loads(response.read().decode("utf-8"))
                items = res_json.get("tracks", {}).get("items", [])
                if items and items[0].get("external_urls", {}).get("spotify"):
                    url = items[0]["external_urls"]["spotify"]
                    _spotify_track_cache[cache_key] = url
                    return url
        except Exception as e:
            logging.warning(f"Spotify search failed for query '{q}': {e}")

    return None


def fetch_apple_music_link(artist: str = None, track: str = None, metadata: dict = None) -> str | None:
    """Resolve Apple Music track URL from metadata, Genius API, or iTunes Search API."""
    if metadata:
        if metadata.get("apple_music_url"):
            return metadata["apple_music_url"]
        if metadata.get("apple_music_id"):
            return f"https://music.apple.com/song/{metadata['apple_music_id']}"

    clean_t = clean_title_for_search(track) if track else ""
    clean_a = (artist or "").strip()
    cache_key = (clean_a.lower(), clean_t.lower())
    if cache_key in _apple_track_cache:
        return _apple_track_cache[cache_key]

    # Try Genius first
    if clean_t:
        try:
            genius_meta = fetch_genius_data(artist=clean_a, track=clean_t, fetch_lyrics=False)
            if genius_meta.get("apple_music_url"):
                url = genius_meta["apple_music_url"]
                _apple_track_cache[cache_key] = url
                return url
            if genius_meta.get("apple_music_id"):
                url = f"https://music.apple.com/song/{genius_meta['apple_music_id']}"
                _apple_track_cache[cache_key] = url
                return url
        except Exception as e:
            logging.debug(f"Genius fetch for Apple Music failed: {e}")

    # Fallback to iTunes Search API
    term = f"{clean_a} {clean_t}".strip() if clean_a else clean_t
    if term:
        try:
            params = urllib.parse.urlencode({"term": term, "entity": "song", "limit": 1})
            search_url = f"https://itunes.apple.com/search?{params}"
            req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read().decode("utf-8"))
                results = data.get("results", [])
                if results and "trackViewUrl" in results[0]:
                    url = results[0]["trackViewUrl"]
                    _apple_track_cache[cache_key] = url
                    return url
        except Exception as e:
            logging.debug(f"iTunes Search API failed: {e}")

    return None


def get_yandex_music_search_url(artist: str = None, track: str = None) -> str | None:
    """Build a Yandex Music deep-link search URL for the given artist and track."""
    clean_t = clean_title_for_search(track) if track else ""
    clean_a = clean_uploader(artist) if artist else ""
    if clean_a:
        clean_t = re.sub(rf'^\s*{re.escape(clean_a)}\s*[-–—:]\s*', '', clean_t, flags=re.I).strip()

    query = f"{clean_a} {clean_t}".strip() or clean_t or clean_a
    if not query:
        return None
    return f"https://music.yandex.ru/search?text={urllib.parse.quote(query)}"


async def build_track_links_markup(
    user_id: int,
    video_id: str = None,
    artist: str = None,
    track: str = None,
    original_url: str = None,
    metadata: dict = None,
    is_inline: bool = False,
) -> InlineKeyboardMarkup | None:
    """
    Build InlineKeyboardMarkup with link buttons to YouTube, Spotify, Apple Music, and/or Yandex Music
    based on the user's settings.
    """
    yt_on = get_user_setting(user_id, "link_youtube", False)
    sp_on = get_user_setting(user_id, "link_spotify", False)
    am_on = get_user_setting(user_id, "link_apple", False)
    ya_on = get_user_setting(user_id, "link_yandex", False)

    if not (yt_on or sp_on or am_on or ya_on):
        return None

    buttons = []
    loop = asyncio.get_running_loop()

    # If metadata is missing but artist or track is present, resolve via Genius first (fetch_lyrics=False is fast)
    if not metadata and (artist or track) and (sp_on or am_on):
        try:
            resolved_meta = await loop.run_in_executor(
                None, lambda: fetch_genius_data(artist=clean_uploader(artist), track=clean_title_for_search(track), fetch_lyrics=False)
            )
            if resolved_meta and resolved_meta.get('matched'):
                metadata = resolved_meta
        except Exception as e:
            logging.debug(f"Genius metadata resolve in build_track_links_markup failed: {e}")

    search_artist = (metadata.get('artist') if metadata and metadata.get('matched') else None) or artist
    search_track = (metadata.get('title') if metadata and metadata.get('matched') else None) or track

    # YouTube Link
    if yt_on and video_id:
        if is_inline:
            buttons.append(InlineKeyboardButton("yt", url=f"https://music.youtube.com/watch?v={video_id}"))
        else:
            buttons.append(InlineKeyboardButton("\u200b", url=f"https://music.youtube.com/watch?v={video_id}",  icon_custom_emoji_id="5302999436085962212"))

    # Resolve Spotify and Apple Music links (looked up at the same time when both are needed)
    spotify_url = None
    if sp_on:
        if original_url and ("open.spotify.com" in original_url or "spotify.com" in original_url):
            spotify_url = original_url
        elif metadata and metadata.get("spotify_url"):
            spotify_url = metadata["spotify_url"]

    apple_url = None
    if am_on:
        if original_url and ("music.apple.com" in original_url or "itunes.apple.com" in original_url):
            apple_url = original_url
        elif metadata and metadata.get("apple_music_url"):
            apple_url = metadata["apple_music_url"]
        elif metadata and metadata.get("apple_music_id"):
            apple_url = f"https://music.apple.com/song/{metadata['apple_music_id']}"

    has_query = bool(search_artist or search_track)
    need_spotify = sp_on and not spotify_url and has_query
    need_apple = am_on and not apple_url and has_query

    async def _lookup(needed: bool, func):
        if not needed:
            return None
        try:
            return await loop.run_in_executor(None, func)
        except Exception as e:
            logging.debug(f"Streaming link lookup failed: {e}")
            return None

    if need_spotify or need_apple:
        found_spotify, found_apple = await asyncio.gather(
            _lookup(need_spotify, lambda: search_spotify_track(artist=search_artist, track=search_track)),
            _lookup(need_apple, lambda: fetch_apple_music_link(artist=search_artist, track=search_track, metadata=metadata)),
        )
        spotify_url = spotify_url or found_spotify
        apple_url = apple_url or found_apple

    # Spotify Link
    if sp_on and spotify_url:
        if is_inline:
            buttons.append(InlineKeyboardButton("sf", url=spotify_url))
        else:
            buttons.append(InlineKeyboardButton("\u200b", url=spotify_url, icon_custom_emoji_id="5303475095124062081"))

    # Apple Music Link
    if am_on and apple_url:
        if is_inline:
            buttons.append(InlineKeyboardButton("ap", url=apple_url))
        else:
            buttons.append(InlineKeyboardButton("\u200b", url=apple_url, icon_custom_emoji_id="5298597854816780205"))

    # Yandex Music Link
    if ya_on:
        yandex_url = None
        if original_url and ("music.yandex." in original_url or "yandex.ru/music" in original_url):
            yandex_url = original_url
        elif metadata and metadata.get("yandex_music_url"):
            yandex_url = metadata["yandex_music_url"]

        if not yandex_url and (search_artist or search_track):
            yandex_url = get_yandex_music_search_url(artist=search_artist, track=search_track)

        if yandex_url:
            if is_inline:
                buttons.append(InlineKeyboardButton("ym", url=yandex_url))
            else:
                buttons.append(InlineKeyboardButton("\u200b", url=yandex_url, icon_custom_emoji_id=YANDEX_ICON_EMOJI_ID))

    if buttons:
        return InlineKeyboardMarkup([buttons])
    return None



# --- HELPER: SEARCH YTMUSIC FOR TRACK ---
def _search_ytmusic_for_track(artist: str, title: str) -> dict | None:
    try:
        clean_title = re.sub(r'[\(\[].*?[\)\]]', '', title).strip()
        results = ytmusic.search(f"{artist} {clean_title}", filter="songs", limit=5)
        for item in results:
            video_id = item.get('videoId')
            if video_id:
                return {
                    'id': video_id,
                    'title': item.get('title', title),
                    'uploader': item.get('artists', [{}])[0].get('name', artist) if item.get('artists') else artist,
                    'duration_string': item.get('duration', '??:??'),
                    'duration': item.get('duration_seconds', 0),
                    'source': '🎵',
                    'cover_url': (item.get('thumbnails') or [{}])[-1].get('url'),
                }
    except Exception as e:
        logging.error(f"YTMusic track search failed: {e}")
    return None


# --- HELPER: ACCURATE SPOTIFY TO YOUTUBE SEARCH ---
def search_spotify_track_on_youtube(artist: str, title: str, spotify_url: str = None) -> dict | None:
    # Strategy 1: Clean-title YTMusic search
    try:
        result = _search_ytmusic_for_track(artist, title)
        if result:
            result['source'] = '🎯'
            return result
    except Exception as e:
        logging.warning(f"Clean-title search failed: {e}")

    # Strategy 2: Scored YTMusic search
    try:
        ytmusic_results = ytmusic.search(f"{artist} {title}", filter="songs", limit=5)
        best_match, best_score = None, 0
        for item in ytmusic_results:
            yt_title = item.get('title', '').lower()
            yt_artist = item.get('artists', [{}])[0].get('name', '').lower() if item.get('artists') else ''
            title_lower, artist_lower = title.lower(), artist.lower()
            score = 0
            if title_lower in yt_title or yt_title in title_lower:
                score += 3
            if artist_lower in yt_artist or yt_artist in artist_lower:
                score += 2
            score += len(set(title_lower.split()) & set(yt_title.split()))
            if score > best_score:
                best_score, best_match = score, item
        if best_match:
            return {
                'id': best_match['videoId'],
                'title': best_match.get('title', title),
                'uploader': best_match.get('artists', [{}])[0].get('name', artist) if best_match.get('artists') else artist,
                'duration_string': best_match.get('duration', '??:??'),
                'duration': best_match.get('duration_seconds', 0),
                'source': '🎵' if best_score >= 5 else '📺',
                'cover_url': (best_match.get('thumbnails') or [{}])[-1].get('url'),
            }
    except Exception as e:
        logging.error(f"Enhanced search failed: {e}")

    return None


def artists_cleaner_hook(d):
    if d['status'] == 'finished':
        info = d.get('info_dict', {})
        if info.get('artists') and isinstance(info['artists'], list) and len(info['artists']) > 0:
            # Keep only the first artist (the scenic name) to avoid real names/composers
            info['artist'] = info['artists'][0]


# --- HELPER: COOKIES MANAGEMENT ---

def _get_cookies_filepath(user_id: int) -> str:
    """Return the path where this user's cookies file would be stored."""
    return os.path.join(COOKIES_DIR, f"{user_id}_cookies.txt")


def _write_cookies_file(user_id: int, cookies_text: str) -> str:
    """Write cookies text to a Netscape-format file and return the path."""
    # Ensure the required Netscape header is present (safety net for
    # cookies saved before the normalization fix was applied)
    if not re.search(r'^#.*HTTP Cookie File', cookies_text, re.MULTILINE):
        cookies_text = '# Netscape HTTP Cookie File\n' + cookies_text
    path = _get_cookies_filepath(user_id)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(cookies_text)
    return path


def _delete_cookies_file(user_id: int):
    """Delete the cookies file for a user if it exists."""
    path = _get_cookies_filepath(user_id)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def _ensure_cookies_file(user_id: int) -> str | None:
    """
    Ensure the cookies file exists on disk for the given user.
    Always re-writes from settings to guarantee freshness.
    Returns the file path, or None if no cookies are set.
    """
    cookies_text = get_user_setting(user_id, "cookies")
    if not cookies_text:
        return None
    path = _get_cookies_filepath(user_id)
    _write_cookies_file(user_id, cookies_text)
    return path


def _normalize_cookies_text(text: str) -> str:
    """Normalize pasted cookies text: ensure Netscape header, convert spaces back to tabs."""
    lines = text.strip().splitlines()
    normalized_lines = []
    has_header = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            if re.match(r'^#.*HTTP Cookie File', stripped):
                has_header = True
            normalized_lines.append(line)
            continue
        if '\t' not in line:
            parts = line.split(maxsplit=6)
            if len(parts) == 7:
                line = '\t'.join(parts)
        normalized_lines.append(line)
    # MozillaCookieJar requires this header — without it, zero cookies are loaded
    if not has_header:
        normalized_lines.insert(0, '# Netscape HTTP Cookie File')
    return '\n'.join(normalized_lines)


_ESSENTIAL_YT_COOKIES = {'SID', 'HSID', 'SSID', 'APISID', 'SAPISID', 'LOGIN_INFO',
                         '__Secure-1PSID', '__Secure-3PSID',
                         '__Secure-1PAPISID', '__Secure-3PAPISID'}


def _validate_cookies_format(text: str) -> tuple[bool, str]:
    """
    Validate that the text looks like a Netscape/Mozilla cookies.txt file.
    Returns (is_valid, error_message).
    """
    lines = text.strip().splitlines()
    if not lines:
        return False, "The cookies text is empty."

    data_lines = [l for l in lines if l.strip() and not l.strip().startswith('#')]
    if not data_lines:
        return False, "No cookie entries found — only comments or blank lines."

    valid_count = 0
    found_cookie_names: set[str] = set()
    for line in data_lines:
        parts = line.split('\t')
        if len(parts) < 7:
            return False, (
                f"Invalid line (expected 7 tab-separated fields, got {len(parts)}):\n"
                f"<code>{html.escape(line[:120])}</code>\n\n"
                "Make sure you're exporting in Netscape/Mozilla format "
                "(tab-separated, not JSON or header format)."
            )
        # Field 4 should be an expiry timestamp (digits)
        expiry = parts[4].strip()
        if expiry and not expiry.isdigit():
            return False, (
                f"Invalid expiry timestamp '<code>{html.escape(expiry)}</code>' in line:\n"
                f"<code>{html.escape(line[:120])}</code>\n\n"
                "This doesn't look like Netscape format cookies."
            )
        found_cookie_names.add(parts[5].strip())
        valid_count += 1

    if valid_count == 0:
        return False, "No valid cookie entries found in the provided text."

    # Check for essential YouTube auth cookies
    found_essential = found_cookie_names & _ESSENTIAL_YT_COOKIES
    if not found_essential:
        return False, (
            f"{valid_count} cookie(s) found, but none are YouTube authentication cookies.\n\n"
            "Expected cookies like <code>SID</code>, <code>LOGIN_INFO</code>, "
            "<code>__Secure-1PSID</code>, etc.\n\n"
            "Make sure you export cookies while logged into YouTube."
        )

    return True, f"{valid_count} cookie(s) parsed ({len(found_essential)} auth cookie(s) found)."


def _verify_cookies_work(cookies_path: str) -> tuple[bool, str]:
    """
    Test if the cookies actually work by trying to extract info from
    a known age-restricted YouTube video.
    Returns (works, message).
    """
    # Use an age-restricted video — this is the whole point of cookies
    test_url = "https://www.youtube.com/watch?v=Tq92D6wQ1mg"
    try:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'noplaylist': True,
            'cookiefile': cookies_path,
            'skip_download': True,
            **base_yt_opts(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(test_url, download=False)
            if info and info.get('title'):
                return True, f"Cookies verified — accessed age-restricted video: {info['title']}"
            return False, "Extraction succeeded but returned no data."
    except yt_dlp.utils.DownloadError as e:
        error_str = str(e)
        logging.warning(f"Cookie verification DownloadError: {error_str}")
        if is_age_restricted_error(error_str):
            return False, (
                "Cookies were not accepted — YouTube still requires sign-in.\n\n"
                "This usually means the cookies are expired, the account "
                "has no birthday set, or the export was incomplete."
            )
        if 'format' in error_str.lower():
            # Format-related errors with cookies = cookies may still be auth-valid
            return True, "Cookies accepted (format test inconclusive, but auth OK)."
        return False, f"Verification failed: {error_str[:200]}"
    except Exception as e:
        logging.warning(f"Cookie verification error: {e}")
        return False, f"Verification failed: {e}"


# --- HELPER: COMMON YT-DLP OPTS ---
def base_yt_opts() -> dict:
    """Options every yt-dlp call needs to look like a real client to YouTube."""
    return {
        # Node.js solves YouTube's JS challenges (signature, n-param)
        'js_runtimes': {'node': {}},
        # PO tokens come from the bgutil HTTP server. extractor_args maps
        # extractor key -> {arg name -> [values]}.
        'extractor_args': {'youtubepot-bgutilhttp': {'base_url': [BGUTIL_URL]}},
    }


def get_ydl_opts(user_id: int = None, use_cookies: bool = True) -> dict:
    # FFmpegExtractAudio only repackages the stream when its codec already matches
    # (no re-encode), so each format prefers the YouTube stream it can keep as-is.
    postprocessor_args = {}
    if AUDIO_FORMAT == 'm4a':
        # YouTube's AAC stream (format 140) goes into .m4a untouched: no encoding time,
        # no generation loss, ~1/3 smaller than 192k MP3. Other codecs get converted to AAC.
        audio_format, codec, quality = 'bestaudio[ext=m4a]/bestaudio/best', 'm4a', '192'
        cookie_format = 'bestaudio[ext=m4a]/bestaudio*/best'
    elif AUDIO_FORMAT == 'opus':
        # YouTube's best audio is already Opus: it is only repackaged into an .opus file.
        audio_format, codec, quality = 'bestaudio[acodec=opus]/bestaudio/best', 'opus', '160'
        cookie_format = 'bestaudio[acodec=opus]/bestaudio*/best'
    else:
        audio_format, codec, quality = 'bestaudio/best', 'mp3', '192'
        cookie_format = 'bestaudio*/best'
        # LAME's "fast" preset: about twice as fast as the default at the same bitrate.
        postprocessor_args = {'extractaudio+ffmpeg_o': ['-compression_level', '7']}
    opts = {
        'format': audio_format,
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        # No writethumbnail: the video thumbnail is only the last cover art fallback,
        # so it is fetched from info['thumbnail'] when it is actually needed.
        'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': codec, 'preferredquality': quality},
            {'key': 'FFmpegMetadata'},
        ],
        'postprocessor_args': postprocessor_args,
        'progress_hooks': [artists_cleaner_hook],
        'quiet': True,
        'noprogress': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'http_chunk_size': 10485760,
        **base_yt_opts(),
    }
    if use_cookies and user_id is not None:
        cookies_path = _ensure_cookies_file(user_id)
        if cookies_path:
            opts['cookiefile'] = cookies_path
            # The * suffix includes formats with video tracks in the audio
            # quality ranking — FFmpegExtractAudio strips video anyway.
            opts['format'] = cookie_format
    return opts


def get_url_info(url: str, user_id: int = None) -> dict:
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True, **base_yt_opts()}
    if user_id is not None:
        cookies_path = _ensure_cookies_file(user_id)
        if cookies_path:
            opts['cookiefile'] = cookies_path
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# --- HELPER: FORMAT DURATION ---
def format_duration(seconds) -> str:
    if seconds is None:
        return "??:??"
    try:
        m, s = divmod(int(seconds), 60)
        return f"{m}:{s:02d}"
    except (ValueError, TypeError):
        return "??:??"


def clean_uploader(name: str) -> str:
    if not name:
        return ""
    # Strip "- Topic", "VEVO", "- Official", "Official" suffixes from channel names
    cleaned = re.sub(r'\s*-\s*Topic\s*$', '', name, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*VEVO\s*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*-\s*Official\s*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*Official\s*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _clean_video_title(title: str, artist: str = None) -> str:
    cleaned = re.sub(
        r'\s*[\(\[][^\)\]]*?\b(official|lyrics?|video|audio|music\s+video|mv|hd|4k|explicit)\b[^\)\]]*[\)\]]',
        '', title, flags=re.IGNORECASE
    ).strip()
    cleaned = re.sub(r'\s*\|.*$', '', cleaned).strip()
    if artist:
        cleaned = re.sub(rf'^\s*{re.escape(artist.strip())}\s*[-–—]\s*', '', cleaned, flags=re.IGNORECASE).strip()
        # Strip trailing "ft. <artist>" / "feat. <artist>" when artist is already the performer
        cleaned = re.sub(r'\s+(?:ft\.?|feat\.?)\s+' + re.escape(artist.strip()) + r'\s*$', '', cleaned, flags=re.IGNORECASE).strip()
    # Strip standalone junk tags not inside parens
    cleaned = re.sub(r'\s*\b(official\s*(music\s*)?video|official\s*audio|lyric\s*video)\b\s*', ' ', cleaned, flags=re.IGNORECASE).strip()
    return cleaned or title


def yt_metadata(info: dict) -> tuple[str, str]:
    # Try structured fields first, then fall back through creator/channel/uploader
    artist = None
    if info.get('artists') and isinstance(info.get('artists'), list) and len(info['artists']) > 0:
        artist = info['artists'][0]

    artist = (
        artist
        or info.get('artist')
        or info.get('album_artist')
        or info.get('creator')
        or clean_uploader(info.get('uploader') or '')
        or clean_uploader(info.get('channel') or '')
        or 'Unknown Artist'
    )
    title = info['track'] if info.get('track') else _clean_video_title(info.get('title') or 'Unknown Track', artist=artist)
    return title, artist


def format_audio_filename(artist: str | None, title: str | None, ext: str = "mp3") -> str:
    """
    Format audio filename as 'artist - track.<ext>' suitable for Telegram audio uploads
    and file downloads, sanitizing characters invalid across file systems.
    """
    artist_clean = (artist or "").strip()
    title_clean = (title or "").strip()

    if artist_clean and title_clean:
        raw_name = f"{artist_clean} - {title_clean}"
    elif title_clean:
        raw_name = title_clean
    elif artist_clean:
        raw_name = artist_clean
    else:
        raw_name = "audio"

    # Replace path separators and colons with dashes/spaces
    sanitized = re.sub(r'[\\/]', '-', raw_name)
    sanitized = re.sub(r'\s*:\s*', ' - ', sanitized)
    # Remove characters forbidden in filenames: < > " | ? *
    sanitized = re.sub(r'[<>\"|?*]', '', sanitized)
    # Remove control characters
    sanitized = re.sub(r'[\x00-\x1f\x7f]', '', sanitized)
    # Normalize consecutive dashes and spaces
    sanitized = re.sub(r'\s+', ' ', sanitized)
    sanitized = re.sub(r'\s*-\s*-\s*', ' - ', sanitized)
    sanitized = sanitized.strip(' .-_')

    if not sanitized:
        sanitized = "audio"

    # Limit filename length (excluding extension) to 200 characters for safety
    if len(sanitized) > 200:
        sanitized = sanitized[:200].rstrip(' .-_')

    return f"{sanitized}.{(ext or 'mp3').lstrip('.').lower()}"



# --- HELPER: DOWNLOAD GUARDS ---
_ERR_EMOJI = '<tg-emoji emoji-id="5348027250446967673">❌</tg-emoji>'


class DownloadRejected(Exception):
    """The bot refuses this download on purpose. user_text is HTML for the chat, short_text for buttons."""

    def __init__(self, user_text: str, short_text: str):
        super().__init__(short_text)
        self.user_text = user_text
        self.short_text = short_text


class TrackTooLongError(DownloadRejected):
    def __init__(self, duration=None):
        limit = MAX_DURATION_SECONDS // 60
        length = f" ({format_duration(duration)})" if duration else ""
        super().__init__(
            f"{_ERR_EMOJI} This track is too long{length}. The limit is {limit} minutes.",
            f"Too long (limit {limit} min)",
        )
        self.duration = duration


def is_too_long(duration) -> bool:
    try:
        return duration is not None and float(duration) > MAX_DURATION_SECONDS
    except (TypeError, ValueError):
        return False


_AGE_RESTRICTED_RE = re.compile(
    r'sign in to confirm your age|age[- ]restrict|confirm your age|inappropriate for some users',
    re.IGNORECASE,
)


def is_age_restricted_error(error_text: str) -> bool:
    return bool(_AGE_RESTRICTED_RE.search(error_text or ""))


# "Sign in to confirm you're not a bot" (YouTube's IP / client reputation check)
_BOT_CHECK_RE = re.compile(r"not a bot|confirm you.?re not", re.IGNORECASE)


def is_bot_check_error(error_text: str) -> bool:
    return bool(_BOT_CHECK_RE.search(error_text or ""))


def describe_download_error(err: Exception, has_cookies: bool) -> tuple[str, str] | None:
    """A friendly (chat HTML, short button text) for known YouTube refusals, else None."""
    text = str(err)
    if is_bot_check_error(text):
        if has_cookies:
            return (f"{_ERR_EMOJI} YouTube asked to confirm this isn't a bot, even with your cookies. "
                    "They have probably expired: export fresh ones and send them with /setcookies.",
                    "YouTube bot check (cookies expired?)")
        return (f"{_ERR_EMOJI} YouTube asked to confirm this isn't a bot. "
                "Set YouTube cookies with /setcookies to get past it.",
                "YouTube bot check (set cookies)")
    if is_age_restricted_error(text):
        if has_cookies:
            return (f"{_ERR_EMOJI} This video is age-restricted and YouTube did not accept your cookies. "
                    "Make sure the account is signed in and its age is verified, then re-send them with /setcookies.",
                    "Age-restricted (cookies rejected)")
        return (f"{_ERR_EMOJI} This video is age-restricted. Set YouTube cookies with /setcookies to download it.",
                "Age-restricted (set cookies)")
    return None


# --- HELPER: ONE DOWNLOAD PER VIDEO AT A TIME ---
# Updates are handled concurrently and all downloads share DOWNLOAD_DIR (files are named by
# video id), so two requests for the same video must not download / delete side by side.
_video_locks: dict = {}


@contextlib.asynccontextmanager
async def video_lock(key: str | None):
    if not key:
        yield
        return
    entry = _video_locks.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _video_locks.pop(key, None)


# --- HELPER: DOWNLOAD SONG BY VIDEO ID ---
def download_song(opts: dict, video_id: str, url: str = None, cancel_event: threading.Event = None,
                  on_info=None) -> dict:
    """on_info(info): called (in this worker thread) once the track is known, before the download."""
    # Use www.youtube.com instead of music.youtube.com to avoid
    # YouTube Music-specific format restrictions with authenticated sessions
    download_url = url or f"https://www.youtube.com/watch?v={video_id}"

    if cancel_event is not None:
        def _cancel_hook(d):
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("Download cancelled (timed out)")
        opts = {**opts, 'progress_hooks': [*opts.get('progress_hooks', []), _cancel_hook]}

    with yt_dlp.YoutubeDL(opts) as ydl:
        # Look first, download second: refuse long tracks, streams and playlists
        # before spending minutes on something Telegram would reject anyway.
        def _check(candidate: dict | None):
            if not candidate:
                raise DownloadRejected(f"{_ERR_EMOJI} Could not read this link.", "Could not read link")
            if candidate.get('_type') in ('playlist', 'multi_video'):
                raise DownloadRejected(
                    f"{_ERR_EMOJI} Playlists and albums are not supported. Send a link to a single track.",
                    "Playlists not supported",
                )
            if candidate.get('is_live'):
                raise DownloadRejected(f"{_ERR_EMOJI} Live streams cannot be downloaded.", "Live stream")
            if is_too_long(candidate.get('duration')):
                raise TrackTooLongError(candidate.get('duration'))

        # process=False returns right after the page is read, so a playlist is refused
        # without visiting every one of its entries.
        raw = ydl.extract_info(download_url, download=False, process=False)
        _check(raw)
        info = ydl.process_ie_result(raw, download=False)
        _check(info)
        if on_info is not None:
            try:
                on_info(info)
            except Exception as e:  # never let a metadata helper break the download
                logging.debug(f"on_info callback failed: {e}")
        if cancel_event is not None and cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled (timed out)")
        return ydl.process_ie_result(info, download=True)


async def run_download(user_id: int, video_id: str, url: str = None, use_cookies: bool = False,
                       timeout: int = 300, on_info=None) -> dict:
    """Run download_song in a worker thread. On timeout the worker is told to stop too."""
    loop = asyncio.get_running_loop()
    cancel_event = threading.Event()
    future = loop.run_in_executor(
        None,
        lambda: download_song(get_ydl_opts(user_id, use_cookies=use_cookies), video_id, url=url,
                              cancel_event=cancel_event, on_info=on_info),
    )
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        cancel_event.set()
        raise


async def download_preferring_cookies(user_id: int, video_id: str | None, url: str = None,
                                      has_cookies: bool = False, on_fallback=None, on_info=None) -> dict:
    """
    Download with the user's cookies when they are set (signed-in requests get far fewer
    bot checks), and fall back to an anonymous download if the signed-in one fails, e.g.
    because the cookies expired or the account sees no usable formats.
    on_fallback: optional coroutine function awaited before the second attempt.
    on_info: passed to download_song (may be called once per attempt).
    """
    if not has_cookies:
        return await run_download(user_id, video_id, url, use_cookies=False, on_info=on_info)
    try:
        return await run_download(user_id, video_id, url, use_cookies=True, on_info=on_info)
    except (DownloadRejected, asyncio.TimeoutError, asyncio.CancelledError):
        raise
    except Exception as cookie_err:
        logging.warning(f"Download with cookies failed for {video_id or url}: {cookie_err}; retrying without")
        await cleanup_files(video_id)
        if on_fallback is not None:
            await on_fallback()
        try:
            return await run_download(user_id, video_id, url, use_cookies=False, on_info=on_info)
        except (DownloadRejected, asyncio.TimeoutError, asyncio.CancelledError):
            raise
        except Exception as plain_err:
            logging.warning(f"Download without cookies also failed for {video_id or url}: {plain_err}")
            # The signed-in attempt is the one that should have worked; its error says more.
            raise cookie_err from plain_err


def _is_file_of(fname: str, video_id: str) -> bool:
    """yt-dlp names everything '<id>.<something>'; an exact prefix avoids touching other videos' files."""
    return fname.startswith(f"{video_id}.")


# --- HELPER: FIND ACTUAL OUTPUT FILE ---
async def find_output_file(directory: str, video_id: str) -> str | None:
    if not video_id:
        return None

    def _find():
        names = [f for f in os.listdir(directory) if _is_file_of(f, video_id)]
        preferred = (f".{AUDIO_FORMAT}",) + tuple(e for e in _AUDIO_EXTENSIONS if e != f".{AUDIO_FORMAT}")
        for ext in preferred:
            for fname in names:
                if fname.lower().endswith(ext):
                    return os.path.join(directory, fname)
        return None
    return await asyncio.to_thread(_find)


_THUMB_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')


def _find_thumbnail(directory: str, video_id: str) -> str | None:
    """Find any thumbnail file for this video, regardless of image format."""
    if not video_id:
        return None
    for fname in os.listdir(directory):
        if _is_file_of(fname, video_id):
            lower = fname.lower()
            if any(lower.endswith(ext) for ext in _THUMB_EXTENSIONS):
                return os.path.join(directory, fname)
    return None


def _process_artwork(raw_bytes: bytes | None) -> tuple[bytes | None, bytes | None]:
    """
    Given raw artwork bytes (JPEG/PNG/WEBP):
    Returns (embed_cover_bytes, telegram_thumb_bytes)
    - embed_cover_bytes: High quality square JPEG (up to 800x800, quality 90) for MP3 ID3 APIC.
    - telegram_thumb_bytes: Telegram-compliant square JPEG (<= 320x320, < 200KB, quality 85).
    Both are cropped from the exact same image to ensure visual consistency between
    Telegram chat and Android notification / media player.
    """
    if not raw_bytes:
        return None, None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw_bytes)) as img:
            # Handle RGBA/transparency
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1])
                img = bg
            else:
                img = img.convert('RGB')

            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            square = img.crop((left, top, left + side, top + side))

            # 1. High-res for ID3 APIC (Android notification/player, up to 800x800)
            embed_img = square
            if side > 800:
                embed_img = square.resize((800, 800), Image.LANCZOS)
            embed_buf = io.BytesIO()
            embed_img.save(embed_buf, format='JPEG', quality=90)
            embed_cover_bytes = embed_buf.getvalue()

            # 2. Telegram Bot API thumbnail (<= 320x320, < 200KB)
            tg_img = square.resize((320, 320), Image.LANCZOS) if side > 320 else square
            tg_buf = io.BytesIO()
            tg_img.save(tg_buf, format='JPEG', quality=85)
            tg_thumb_bytes = tg_buf.getvalue()

            return embed_cover_bytes, tg_thumb_bytes
    except ImportError:
        # Without Pillow we can only safely use bytes that are already JPEG
        if raw_bytes[:3] == b'\xff\xd8\xff':
            return raw_bytes, (raw_bytes if len(raw_bytes) < 200 * 1024 else None)
        return None, None
    except Exception as e:
        # Corrupt/non-image payload (e.g. HTML error page) — never pass it on,
        # Telegram rejects invalid thumbnails and the whole upload would fail.
        logging.warning(f"Artwork processing failed: {e}")
        return None, None


def _fetch_artwork_from_url(url: str, timeout: int = 8) -> bytes | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MusicBot/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logging.debug(f"Artwork fetch from URL failed ({url}): {e}")
        return None


def fetch_genius_cover(artist: str = None, track: str = None) -> str | None:
    """
    Search Genius API to fetch the high-resolution cover art URL for a track.
    Returns the cover art URL string or None. Skips lyrics scraping.
    """
    if not artist and not track:
        return None
    try:
        data = fetch_genius_data(artist=artist, track=track, fetch_lyrics=False)
        return data.get('cover_url') if data else None
    except Exception as e:
        logging.debug(f"Genius cover URL lookup failed: {e}")
        return None


_GOOGLE_IMG_SIZE_RE = re.compile(r'=w\d+-h\d+[^/?#]*$')


def _upscale_cover_url(url: str | None) -> str | None:
    """
    YouTube Music search returns tiny (60-120px) lh3.googleusercontent.com /
    yt3.ggpht.com thumbnails. Rewrite the size suffix to request a large image.
    """
    if not url:
        return url
    if 'googleusercontent.com' in url or 'ggpht.com' in url:
        if _GOOGLE_IMG_SIZE_RE.search(url):
            return _GOOGLE_IMG_SIZE_RE.sub('=w1200-h1200-l90-rj', url)
    return url


def _read_local_thumbnail(video_id: str) -> bytes | None:
    path = _find_thumbnail(DOWNLOAD_DIR, video_id)
    if path:
        try:
            with open(path, 'rb') as f:
                return f.read()
        except Exception:
            pass
    return None


async def _get_artwork_pair(
    video_id: str,
    artist: str = None,
    track: str = None,
    cover_url: str = None,
    genius_cover_url: str = None,
    genius_checked: bool = False,
    thumbnail_url: str = None,
) -> tuple[bytes | None, bytes | None]:
    """
    Get the synchronized (embed_cover_bytes, telegram_thumb_bytes) pair.

    cover_url        -- provider artwork (Spotify / YouTube Music CDN), NOT Genius.
    genius_cover_url -- Genius artwork already resolved by the metadata pipeline.
    genius_checked   -- True if Genius already matched this track, so a second
                        lookup would be redundant (e.g. matched but no artwork).
    thumbnail_url    -- the video's own thumbnail (yt-dlp info['thumbnail']).

    Priority (first source that yields a valid image wins):
      1. Genius high-resolution cover art (default)
      2. Direct provider cover_url
      3. The video thumbnail (a local file if one exists, else thumbnail_url), cropped to square
    """
    loop = asyncio.get_running_loop()

    def _genius_source():
        url = genius_cover_url
        if not url and not genius_checked and (artist or track):
            url = fetch_genius_cover(artist=artist, track=track)
        return _fetch_artwork_from_url(url) if url else None

    sources = [_genius_source]
    if cover_url and cover_url != genius_cover_url:
        provider_url = _upscale_cover_url(cover_url)

        def _provider_source():
            data = _fetch_artwork_from_url(provider_url)
            if not data and provider_url != cover_url:
                data = _fetch_artwork_from_url(cover_url)
            return data
        sources.append(_provider_source)
    if video_id or thumbnail_url:
        def _thumbnail_source():
            data = _read_local_thumbnail(video_id) if video_id else None
            if not data and thumbnail_url and thumbnail_url not in (cover_url, genius_cover_url):
                data = _fetch_artwork_from_url(thumbnail_url)
            return data
        sources.append(_thumbnail_source)

    for source in sources:
        raw_bytes = await loop.run_in_executor(None, source)
        if not raw_bytes:
            continue
        embed_bytes, tg_bytes = await loop.run_in_executor(None, lambda: _process_artwork(raw_bytes))
        if embed_bytes or tg_bytes:
            return embed_bytes, tg_bytes

    return None, None


async def _get_thumbnail_bytes(video_id: str, artist: str = None, track: str = None, cover_url: str = None) -> bytes | None:
    """Convenience wrapper returning telegram thumb bytes."""
    _, tg_bytes = await _get_artwork_pair(video_id, artist, track, cover_url)
    return tg_bytes


def _prepare_and_embed_metadata(
    file_path: str,
    title: str = None,
    artist: str = None,
    cover_bytes: bytes = None,
    album: str = None,
    album_artist: str = None,
    release_date: str = None,
    lyrics: str = None,
    composer: str = None,
    lyricist: str = None,
) -> None:
    """
    Embeds clean ID3v2.3 tags (TIT2, TPE1, APIC, TALB, TPE2, TDRC, USLT, TCOM, TEXT) into the MP3 file.
    Removes existing conflicting APIC and USLT frames and saves in ID3v2.3 format
    so that Android MediaSession / Pixel notification and lockscreen players
    and desktop music players consistently read the exact same artwork, metadata, and lyrics.
    Opus files get the same information as Vorbis comments, M4A files as MP4 atoms.
    """
    other_writer = None
    if file_path.lower().endswith(('.opus', '.ogg')):
        other_writer = _embed_opus_metadata
    elif file_path.lower().endswith(('.m4a', '.mp4')):
        other_writer = _embed_m4a_metadata
    if other_writer is not None:
        other_writer(
            file_path, title=title, artist=artist, cover_bytes=cover_bytes, album=album,
            album_artist=album_artist, release_date=release_date, lyrics=lyrics,
            composer=composer, lyricist=lyricist,
        )
        return
    try:
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if album_artist:
            tags.add(TPE2(encoding=3, text=album_artist))
        if release_date:
            tags.add(TDRC(encoding=3, text=str(release_date)))
        if composer:
            tags.add(TCOM(encoding=3, text=composer))
        if lyricist:
            tags.add(TEXT(encoding=3, text=lyricist))
        if lyrics:
            tags.delall('USLT')
            tags.add(USLT(encoding=3, lang='eng', desc='', text=lyrics))

        if cover_bytes:
            # Delete any existing APIC frames to prevent conflict
            tags.delall('APIC')
            tags.add(
                APIC(
                    encoding=0,  # 0 = Latin-1 (standard for ID3v2.3 APIC)
                    mime='image/jpeg',
                    type=3,      # 3 = Cover (front)
                    desc='Cover',
                    data=cover_bytes
                )
            )

        tags.save(file_path, v2_version=3)
        logging.debug(f"Successfully embedded clean ID3v2.3 metadata into {file_path}")
    except Exception as e:
        logging.warning(f"Failed to embed metadata/album art into {file_path}: {e}")


def _embed_m4a_metadata(
    file_path: str,
    title: str = None,
    artist: str = None,
    cover_bytes: bytes = None,
    album: str = None,
    album_artist: str = None,
    release_date: str = None,
    lyrics: str = None,
    composer: str = None,
    lyricist: str = None,
) -> None:
    """Write title / artist / album / lyrics / cover art into an M4A (MP4) file as iTunes-style atoms."""
    try:
        from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

        audio = MP4(file_path)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        # yt-dlp copies the whole video description into the file; it is not music metadata.
        for junk in ('desc', 'ldes', '\xa9cmt', 'purl', 'egid'):
            tags.pop(junk, None)

        fields = {
            '\xa9nam': title, '\xa9ART': artist, '\xa9alb': album, 'aART': album_artist,
            '\xa9day': str(release_date) if release_date else None,
            '\xa9wrt': composer, '\xa9lyr': lyrics,
        }
        for key, value in fields.items():
            if value:
                tags[key] = [value]
        if lyricist:
            # No standard atom for the lyricist: the freeform key Picard / MusicBrainz use.
            tags['----:com.apple.iTunes:LYRICIST'] = [
                MP4FreeForm(lyricist.encode('utf-8'), dataformat=MP4FreeForm.FORMAT_TEXT)
            ]
        if cover_bytes:
            fmt = MP4Cover.FORMAT_PNG if cover_bytes[:4] == b'\x89PNG' else MP4Cover.FORMAT_JPEG
            tags['covr'] = [MP4Cover(cover_bytes, imageformat=fmt)]

        audio.save()
        logging.debug(f"Successfully embedded MP4 metadata into {file_path}")
    except Exception as e:
        logging.warning(f"Failed to embed metadata/album art into {file_path}: {e}")


def _embed_opus_metadata(
    file_path: str,
    title: str = None,
    artist: str = None,
    cover_bytes: bytes = None,
    album: str = None,
    album_artist: str = None,
    release_date: str = None,
    lyrics: str = None,
    composer: str = None,
    lyricist: str = None,
) -> None:
    """Write title / artist / album / lyrics / cover art into an Ogg Opus file."""
    try:
        from mutagen.oggopus import OggOpus
        from mutagen.flac import Picture

        audio = OggOpus(file_path)
        # yt-dlp copies the whole video description into the file; it is not music metadata.
        for junk in ('description', 'synopsis', 'comment'):
            if junk in audio:
                del audio[junk]

        fields = {
            'title': title, 'artist': artist, 'album': album, 'albumartist': album_artist,
            'date': str(release_date) if release_date else None,
            'composer': composer, 'lyricist': lyricist, 'lyrics': lyrics,
        }
        for key, value in fields.items():
            if value:
                audio[key] = [value]

        if cover_bytes:
            picture = Picture()
            picture.type = 3  # Cover (front)
            picture.mime = 'image/jpeg'
            picture.desc = 'Cover'
            picture.data = cover_bytes
            try:
                from PIL import Image
                with Image.open(io.BytesIO(cover_bytes)) as img:
                    picture.width, picture.height = img.size
                picture.depth = 24
            except Exception:
                pass
            audio['metadata_block_picture'] = [base64.b64encode(picture.write()).decode('ascii')]

        audio.save()
        logging.debug(f"Successfully embedded Opus metadata into {file_path}")
    except Exception as e:
        logging.warning(f"Failed to embed metadata/album art into {file_path}: {e}")


async def cleanup_files(video_id: str | None):
    if not video_id:
        return
    def _cleanup():
        for fname in os.listdir(DOWNLOAD_DIR):
            if _is_file_of(fname, video_id):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, fname))
                except Exception:
                    pass
    await asyncio.to_thread(_cleanup)



# --- HELPER: HYBRID SEARCH ---
async def search_hybrid(query, user_id: int = None):
    cache_key = " ".join(query.lower().split())
    cached_results = HYBRID_SEARCH_CACHE.get(cache_key)
    if cached_results:
        return [dict(r) for r in cached_results]

    combined_results = []
    seen_ids = set()
    loop = asyncio.get_running_loop()

    def _ytmusic_search():
        try:
            return ytmusic.search(query, filter="songs", limit=3)[:3]
        except Exception as e:
            logging.error(f"YTMusic Search failed: {e}")
            return []

    def _youtube_search():
        try:
            opts = {'quiet': True, 'noplaylist': True, 'extract_flat': True, **base_yt_opts()}
            cookies_path = _ensure_cookies_file(user_id) if user_id else None
            if cookies_path:
                opts['cookiefile'] = cookies_path
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch10:{query}", download=False)
                return info.get('entries', [])
        except Exception as e:
            logging.error(f"YouTube Search failed: {e}")
            return []

    music_results, yt_entries = await asyncio.gather(
        loop.run_in_executor(None, _ytmusic_search),
        loop.run_in_executor(None, _youtube_search)
    )

    for item in music_results:
        video_id = item.get('videoId')
        if not video_id or video_id in seen_ids:
            continue
        if is_too_long(item.get('duration_seconds')):
            continue
        seen_ids.add(video_id)
        cover_url = None
        if item.get('thumbnails'):
            cover_url = item['thumbnails'][-1].get('url')
        combined_results.append({
            'id': video_id,
            'title': item['title'],
            'uploader': item['artists'][0]['name'] if 'artists' in item else "Unknown",
            'duration_string': item.get('duration', '??:??'),
            'duration': item.get('duration_seconds', 0),
            'source': '🎵',
            'cover_url': cover_url,
        })

    needed = max(0, 10 - len(combined_results))
    for item in yt_entries:
        if needed <= 0:
            break
        video_id = item.get('id')
        if not video_id or video_id in seen_ids:
            continue
        if is_too_long(item.get('duration')):
            continue
        seen_ids.add(video_id)
        cover_url = None
        if item.get('thumbnails'):
            cover_url = item['thumbnails'][-1].get('url')
        elif item.get('thumbnail'):
            cover_url = item['thumbnail']
        combined_results.append({
            'id': video_id,
            'title': item.get('title') or 'Unknown Track',
            'uploader': clean_uploader(item.get('uploader') or 'YouTube'),
            'duration_string': format_duration(item.get('duration')),
            'duration': item.get('duration', 0),
            'source': '📺',
            'cover_url': cover_url,
        })
        needed -= 1

    if combined_results:
        HYBRID_SEARCH_CACHE[cache_key] = [dict(r) for r in combined_results]
    return combined_results


# --- HELPER: BUILD DISPLAY ---
def build_display(results, page, total_pages):
    items = results[page * 5:(page + 1) * 5]

    def fmt(idx, item):
        src, title, uploader, dur = item['source'], item['title'], item['uploader'], item['duration_string']
        prefix = f"{idx + 1}." if src == '📺' else f"{idx + 1}. <tg-emoji emoji-id=\"5891249688933305846\">🎵</tg-emoji>"
        return f"{prefix} <b>{html.escape(str(title))}</b>\n    └ {html.escape(str(uploader))} ({html.escape(str(dur))})"

    message_text = "\n\n".join(fmt(i, it) for i, it in enumerate(items))
    message_text += f"\n\n<tg-emoji emoji-id=\"5956561916573782596\">📄</tg-emoji> <i>Page {page+1} of {total_pages}</i>"

    number_row = [InlineKeyboardButton(str(i + 1), callback_data=f"dl:{it['id']}") for i, it in enumerate(items)]
    nav_row = [
        InlineKeyboardButton("\u200b", callback_data=f"page:{page-1}", icon_custom_emoji_id="5386670230850267660", style="primary") if page > 0 else InlineKeyboardButton("\u200b", callback_data="noop", icon_custom_emoji_id="5872829476143894491"),
        InlineKeyboardButton("Cancel",callback_data="cancel", style="danger", icon_custom_emoji_id="5287611315588707430"),
        InlineKeyboardButton("\u200b", callback_data=f"page:{page+1}", icon_custom_emoji_id="5386630210345003204", style="primary") if page < total_pages - 1 else InlineKeyboardButton("\u200b", callback_data="noop", icon_custom_emoji_id="5872829476143894491"),
    ]
    return message_text, InlineKeyboardMarkup([number_row, nav_row])


# --- HELPER: SEND AUDIO (WITH CACHE) ---
async def send_downloaded_audio(
    update_or_query, context, user_id: int, video_id: str,
    song_title: str, uploader: str, duration: int,
    original_url: str = None, title_hint: str = None,
    caption=None,
    cover_url: str = None,
    lyrics: str = None,
    embed_lyrics: str = None,
    metadata: dict = None,
    reply_markup: InlineKeyboardMarkup = None,
    thumbnail_url: str = None,
) -> bool:
    """Send audio to the user, using DB cache if available."""
    loop = asyncio.get_running_loop()

    if hasattr(update_or_query, 'effective_chat') and update_or_query.effective_chat:
        chat_id = update_or_query.effective_chat.id
    else:
        chat_id = update_or_query.message.chat_id

    # Check the cache first: a cached file is sent as-is, so it needs no embed lyrics / artwork.
    cached = await get_cached_audio(video_id)
    is_cached = bool(cached and cached.tg_file_id)

    separate_lyrics = None
    if get_user_setting(user_id, "include_lyrics", False):
        if not lyrics:
            meta = await loop.run_in_executor(
                None, lambda: fetch_genius_data(artist=uploader, track=song_title)
            )
            lyrics = meta.get('lyrics')
            if not metadata:
                metadata = meta
            if not embed_lyrics:
                embed_lyrics = meta.get('embed_lyrics')

        if lyrics:
            separate_lyrics = lyrics

    # Lyrics for embedding into a freshly downloaded file: ALWAYS LRCLIB, regardless of user settings
    if not is_cached and not embed_lyrics:
        meta_dict = metadata or {}
        embed_lyrics = meta_dict.get('embed_lyrics')
        if not embed_lyrics:
            embed_lyrics = await loop.run_in_executor(
                None, lambda: fetch_lrclib_lyrics(uploader, song_title)
            )
            if not embed_lyrics and lyrics:
                embed_lyrics = lyrics

    if reply_markup is None:
        reply_markup = await build_track_links_markup(
            user_id=user_id,
            video_id=video_id,
            artist=uploader,
            track=song_title,
            original_url=original_url,
            metadata=metadata,
        )

    if is_cached:
        logging.info(f"Sending from cache for video_id: {video_id}")
        await context.bot.send_audio(
            chat_id=chat_id, audio=cached.tg_file_id,
            title=song_title, performer=uploader, duration=duration,
            caption=caption, parse_mode='HTML',
            filename=format_audio_filename(uploader, song_title),
            reply_markup=reply_markup,
            read_timeout=120, write_timeout=120
        )
        if separate_lyrics:
            await _send_lyrics_message(context.bot, chat_id, uploader, song_title, separate_lyrics)
        return True

    actual_file = await find_output_file(DOWNLOAD_DIR, video_id)
    if not actual_file:
        return False

    # Get synchronized artwork pair (embed_cover_bytes for MP3 ID3, tg_thumb_bytes for Telegram message)
    # `metadata` is Genius data (from the metadata bundle / fetch_genius_data);
    # `cover_url` is the provider (Spotify / YouTube Music) artwork fallback.
    genius_meta = metadata or {}
    embed_bytes, tg_thumb_bytes = await _get_artwork_pair(
        video_id, artist=uploader, track=song_title, cover_url=cover_url,
        genius_cover_url=genius_meta.get('cover_url'),
        genius_checked=bool(genius_meta.get('matched')),
        thumbnail_url=thumbnail_url,
    )

    # Embed clean ID3v2.3 tags and album art into MP3 file
    meta_dict = metadata or {}
    await asyncio.to_thread(
        _prepare_and_embed_metadata,
        actual_file,
        title=song_title,
        artist=uploader,
        cover_bytes=embed_bytes,
        album=meta_dict.get('album'),
        album_artist=meta_dict.get('artist') or uploader,
        release_date=meta_dict.get('release_date'),
        lyrics=embed_lyrics,
        composer=meta_dict.get('composer'),
        lyricist=meta_dict.get('lyricist'),
    )

    thumb_file = None
    if tg_thumb_bytes:
        thumb_file = io.BytesIO(tg_thumb_bytes)
        thumb_file.name = "thumbnail.jpg"

    audio_filename = format_audio_filename(uploader, song_title, ext=os.path.splitext(actual_file)[1])

    with open(actual_file, 'rb') as audio_file:
        sent_msg = await context.bot.send_audio(
            chat_id=chat_id, audio=audio_file,
            title=song_title, performer=uploader, duration=duration,
            caption=caption, thumbnail=thumb_file, parse_mode='HTML',
            filename=audio_filename,
            reply_markup=reply_markup,
            read_timeout=120, write_timeout=120
        )
        # Only cache real audio messages; anything else could not be re-sent as audio later.
        tg_file_id = sent_msg.audio.file_id if sent_msg.audio else None
        if not tg_file_id:
            logging.warning(f"Telegram did not store {video_id} as audio; not caching it.")

        if DUMP_CHAT_ID and tg_file_id:
            try:
                await context.bot.send_audio(
                    chat_id=DUMP_CHAT_ID, audio=tg_file_id,
                    title=song_title, performer=uploader, duration=duration,
                    filename=audio_filename,
                )
                await save_cached_audio(video_id, tg_file_id, song_title, uploader, duration)
                logging.info(f"Cached audio for video_id: {video_id}")
            except Exception as e:
                logging.warning(f"Failed to cache audio to dump chat: {e}")

    if separate_lyrics:
        await _send_lyrics_message(context.bot, chat_id, uploader, song_title, separate_lyrics)

    return True


# --- HELPER: UNIFIED DOWNLOAD AND UPLOAD FLOW ---
async def process_download_and_upload(
    update_or_query, context, user_id: int, video_id: str,
    status_msg,
    fallback_title: str = None, fallback_artist: str = None,
    original_url: str = None,
    download_url: str = None,
    delete_status_on_success: bool = True,
    authoritative: bool = False,
    cover_url: str = None,
) -> bool:
    """
    Download, tag, upload and clean up one track. Only one request per video runs at a time;
    a second request for the same video waits and is then served from the cache.
    """
    state = {'video_id': video_id}
    async with video_lock(video_id or download_url):
        try:
            return await _process_download_and_upload(
                update_or_query, context, user_id, video_id, status_msg,
                fallback_title=fallback_title, fallback_artist=fallback_artist,
                original_url=original_url, download_url=download_url,
                delete_status_on_success=delete_status_on_success,
                authoritative=authoritative, cover_url=cover_url, state=state,
            )
        finally:
            # Still inside the lock, so this can never delete another request's files.
            await cleanup_files(state['video_id'])


async def _process_download_and_upload(
    update_or_query, context, user_id: int, video_id: str,
    status_msg,
    fallback_title: str = None, fallback_artist: str = None,
    original_url: str = None,
    download_url: str = None,
    delete_status_on_success: bool = True,
    authoritative: bool = False,
    cover_url: str = None,
    state: dict = None,
) -> bool:
    """
    authoritative=True: fallback_title/fallback_artist are trusted (e.g. from stats.fm or a
    resolved link) and always win over yt-dlp metadata.
    authoritative=False: Genius metadata is tried first; fallback is used only when
    yt-dlp has no structured fields.

    Metadata priority:
      1. Authoritative caller data (stats.fm / resolved link)  [when authoritative=True]
      2. Genius matched metadata (official title, artist, album, high-res 1000x1000 cover)
      3. yt-dlp structured fields  (track / artist — set on YouTube Music videos)
      4. yt-dlp cleaned video title / uploader  (last resort)
    """
    include_lyrics = get_user_setting(user_id, "include_lyrics", False)

    # Someone else may have downloaded this exact video while we waited for the lock.
    if video_id:
        cached_audio = await get_cached_audio(video_id)
        if cached_audio and cached_audio.tg_file_id:
            try:
                sent = await send_downloaded_audio(
                    update_or_query, context, user_id, video_id,
                    fallback_title or cached_audio.title or 'Unknown Track',
                    fallback_artist or cached_audio.performer or 'Unknown Artist',
                    cached_audio.duration,
                    original_url=original_url, cover_url=cover_url,
                )
                if sent and delete_status_on_success:
                    await status_msg.delete()
                return sent
            except Exception as e:
                logging.error(f"Cache send error: {e}")
                await status_msg.edit_text(f"{_ERR_EMOJI} Error: {html.escape(str(e))}", parse_mode='HTML')
                return False

    # Launch Genius and lyrics retrieval concurrently with the yt-dlp download!
    meta_future = asyncio.get_running_loop().run_in_executor(
        None, lambda: _fetch_metadata_bundle(
            video_id=video_id,
            artist=fallback_artist,
            track=fallback_title,
            original_url=original_url,
            include_lyrics=include_lyrics,
        )
    )

    await status_msg.edit_text(text="<tg-emoji emoji-id=\"5292226786229236118\">🔄</tg-emoji> Downloading...", parse_mode='HTML')

    has_cookies = bool(_ensure_cookies_file(user_id)) if user_id else False

    try:
        loop = asyncio.get_running_loop()

        # Cookies first when they are set, anonymous download as the fallback.
        async def _announce_fallback():
            await status_msg.edit_text(
                text="<tg-emoji emoji-id=\"5292226786229236118\">🔄</tg-emoji> Retrying without cookies...",
                parse_mode='HTML'
            )

        # Without a known title (plain links) the first metadata lookup has nothing to
        # search for. Start the real one as soon as yt-dlp has read the page, so it runs
        # alongside the audio download instead of after it.
        early_lookup: dict = {}

        def _start_early_lookup(found_info: dict):  # runs in the yt-dlp worker thread
            if fallback_title or 'future' in early_lookup:
                return
            found_title, found_artist = yt_metadata(found_info)
            early_lookup['future'] = _META_POOL.submit(
                _fetch_metadata_bundle,
                video_id=video_id or found_info.get('id'),
                artist=found_artist,
                track=found_title,
                original_url=original_url,
                include_lyrics=include_lyrics,
            )

        info = None
        try:
            info = await download_preferring_cookies(user_id, video_id, download_url,
                                                     has_cookies=has_cookies, on_fallback=_announce_fallback,
                                                     on_info=_start_early_lookup)
        except DownloadRejected:
            raise
        except asyncio.TimeoutError:
            logging.error(f"Download timed out for video_id: {video_id}")
            await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Download timed out. The file may be too large or the connection is slow.", parse_mode='HTML')
            return False
        except Exception as download_err:
            known = describe_download_error(download_err, has_cookies)
            if known is None:
                raise
            logging.error(f"Download refused by YouTube for {video_id}: {download_err}")
            await status_msg.edit_text(known[0], parse_mode='HTML')
            return False

        # Links to other sites (e.g. SoundCloud) only reveal their id after extraction.
        if not video_id:
            video_id = str(info.get('id') or '') or None
            if state is not None:
                state['video_id'] = video_id

        info_title, info_artist = yt_metadata(info)

        # Collect metadata bundle — download was the bottleneck, bundle should be ready
        meta_bundle: dict = {}
        if meta_future is not None:
            try:
                meta_bundle = await asyncio.wait_for(asyncio.shield(meta_future), timeout=4)
            except (asyncio.TimeoutError, Exception) as e:
                logging.debug(f"Metadata bundle wait error: {e}")

        genius_data = meta_bundle.get('genius', {})
        lyrics = meta_bundle.get('lyrics')
        embed_lyrics = meta_bundle.get('embed_lyrics')

        # If fallback_title was not known beforehand (e.g. raw YouTube link),
        # and Genius was not matched yet, attempt quick match using info_title / info_artist
        if not genius_data.get('matched') and (info_title or info_artist):
            try:
                if 'future' in early_lookup:
                    late_bundle = await asyncio.wrap_future(early_lookup['future'])
                else:
                    late_bundle = await loop.run_in_executor(
                        None, lambda: _fetch_metadata_bundle(
                            video_id=video_id,
                            artist=info_artist,
                            track=info_title,
                            original_url=original_url,
                            include_lyrics=include_lyrics,
                        )
                    )
                if late_bundle.get('genius', {}).get('matched'):
                    genius_data = late_bundle['genius']
                    if not lyrics:
                        lyrics = late_bundle.get('lyrics')
                    if not embed_lyrics:
                        embed_lyrics = late_bundle.get('embed_lyrics')
            except Exception as e:
                logging.debug(f"Late Genius fetch error: {e}")

        # Apply metadata priority
        if authoritative:
            song_title = fallback_title or genius_data.get('title') or info_title
            uploader   = fallback_artist or genius_data.get('artist') or info_artist
        else:
            song_title = genius_data.get('title') or (fallback_title if fallback_title and fallback_title != 'Unknown Track' else info_title)
            uploader   = genius_data.get('artist') or (fallback_artist if fallback_artist and fallback_artist != 'Unknown Artist' else info_artist)

        duration = info.get('duration')

        caption = None

        await status_msg.edit_text(text="<tg-emoji emoji-id=\"5292226786229236118\">🔄</tg-emoji> Uploading...", parse_mode='HTML')

        # Genius cover (via metadata=genius_data) is preferred inside
        # send_downloaded_audio; cover_url stays the provider fallback.
        success = await send_downloaded_audio(
            update_or_query, context, user_id, video_id,
            song_title, uploader, duration,
            original_url=original_url,
            title_hint=f"{uploader} {song_title}",
            caption=caption,
            cover_url=cover_url,
            lyrics=lyrics,
            embed_lyrics=embed_lyrics,
            metadata=genius_data,
            thumbnail_url=info.get('thumbnail'),
        )

        if success:
            if delete_status_on_success:
                await status_msg.delete()
        else:
            await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: File not found after download.", parse_mode='HTML')
        return success


    except DownloadRejected as e:
        logging.info(f"Download refused for {video_id}: {e}")
        await status_msg.edit_text(e.user_text, parse_mode='HTML')
        return False
    except Exception as e:
        logging.error(f"Download/Upload flow error: {e}")
        await status_msg.edit_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: {html.escape(str(e))}", parse_mode='HTML')
        return False


# --- HELPER: GET CURRENT/LAST PLAYED TRACKS FROM STATS.FM ---
async def get_statsfm_tracks_info(user_id: int) -> list[dict]:
    username = get_user_setting(user_id, "stats_fm_username")
    if not username or username == "None":
        return []

    try:
        loop = asyncio.get_running_loop()
        current_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/current"
        data = await asyncio.wait_for(loop.run_in_executor(None, lambda: _http_get_json(current_url)), timeout=15)

        results = []
        
        def parse_track(track_info, status_text):
            track_name = track_info.get("name", "")
            artists = track_info.get("artists", [])
            artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name")) if artists else ""
            
            spotify_id = None
            external_spotify = track_info.get("externalIds", {}).get("spotify", [])
            if external_spotify:
                if isinstance(external_spotify, list) and len(external_spotify) > 0:
                    spotify_id = external_spotify[0]
                elif isinstance(external_spotify, str):
                    spotify_id = external_spotify
            if not spotify_id and track_info.get("spotifyId"):
                spotify_id = track_info.get("spotifyId")
                
            spotify_url = f"https://open.spotify.com/track/{spotify_id}" if spotify_id else None
            return {
                "track_name": track_name,
                "artist_name": artist_name,
                "search_query": f"{artist_name} {track_name}".strip(),
                "spotify_url": spotify_url,
                "status_text": status_text,
                "has_statsfm": True
            }

        if data and data.get("item"):
            track_info = data["item"].get("track", {})
            if track_info:
                results.append(parse_track(track_info, "(stats.fm)"))
        else:
            recent_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/recent"
            recent_data = await asyncio.wait_for(loop.run_in_executor(None, lambda: _http_get_json(recent_url)), timeout=15)
            if recent_data and recent_data.get("items"):
                seen = set()
                for item in recent_data["items"]:
                    track_info = item.get("track", {})
                    if track_info:
                        t = parse_track(track_info, "(recent)")
                        key = (t["artist_name"], t["track_name"])
                        if key not in seen:
                            seen.add(key)
                            results.append(t)
                            if len(results) >= 3:
                                break

        return results
    except Exception as e:
        logging.error(f"Error getting stats.fm tracks info: {e}")
        return []


# --- HELPER: GET CURRENT/LAST PLAYED TRACKS FROM LAST.FM ---
async def get_lastfm_tracks_info(user_id: int) -> list[dict]:
    username = get_user_setting(user_id, "lastfm_username")
    if not username or username == "None" or not LASTFM_API_KEY:
        return []

    try:
        loop = asyncio.get_running_loop()
        url = f"https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user={urllib.parse.quote(username)}&api_key={LASTFM_API_KEY}&format=json&limit=5"
        data = await asyncio.wait_for(loop.run_in_executor(None, lambda: _http_get_json(url)), timeout=15)
        if not data or not isinstance(data, dict):
            return []

        raw_tracks = data.get("recenttracks", {}).get("track", [])
        if isinstance(raw_tracks, dict):
            raw_tracks = [raw_tracks]

        results = []
        seen = set()
        for track in raw_tracks:
            track_name = track.get("name", "")
            artist = track.get("artist", {})
            artist_name = artist.get("#text", "") if isinstance(artist, dict) else str(artist)
            if not track_name or not artist_name:
                continue

            is_now_playing = track.get("@attr", {}).get("nowplaying") == "true"
            status_text = "(last.fm)" if is_now_playing else "(recent)"

            cover_url = None
            images = track.get("image", [])
            if isinstance(images, list):
                for img in reversed(images):
                    if isinstance(img, dict) and img.get("#text"):
                        cover_url = img["#text"]
                        break

            key = (artist_name.lower(), track_name.lower())
            if key not in seen:
                seen.add(key)
                results.append({
                    "track_name": track_name,
                    "artist_name": artist_name,
                    "search_query": f"{artist_name} {track_name}".strip(),
                    "spotify_url": None,
                    "status_text": status_text,
                    "has_lastfm": True,
                    "cover_url": cover_url,
                    "is_now_playing": is_now_playing,
                })
                if len(results) >= 3:
                    break

        return results
    except Exception as e:
        logging.error(f"Error getting last.fm tracks info: {e}")
        return []


def _fetch_statsfm_now_playing(username: str) -> dict | None:
    try:
        current_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/current"
        data = _http_get_json(current_url, timeout=10)
        track_info, status_text = None, None
        if data and data.get("item"):
            track_info = data["item"].get("track", {})
            status_text = "Currently playing"
        else:
            recent_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/recent"
            recent_data = _http_get_json(recent_url, timeout=10)
            if recent_data and recent_data.get("items"):
                track_info = recent_data["items"][0].get("track", {})
                status_text = "Last played"

        if not track_info:
            return None

        track_name = track_info.get("name", "")
        artists = track_info.get("artists", [])
        artist_name = ", ".join(a.get("name", "") for a in artists if a.get("name")) if artists else ""

        spotify_id = None
        external_spotify = track_info.get("externalIds", {}).get("spotify", [])
        if external_spotify:
            if isinstance(external_spotify, list) and len(external_spotify) > 0:
                spotify_id = external_spotify[0]
            elif isinstance(external_spotify, str):
                spotify_id = external_spotify
        if not spotify_id and track_info.get("spotifyId"):
            spotify_id = track_info.get("spotifyId")

        spotify_url = f"https://open.spotify.com/track/{spotify_id}" if spotify_id else None
        return {
            "track_name": track_name,
            "artist_name": artist_name,
            "status_text": status_text,
            "is_now_playing": (status_text == "Currently playing"),
            "spotify_url": spotify_url,
            "cover_url": None,
            "service": "stats.fm",
        }
    except Exception as e:
        logging.error(f"Error fetching stats.fm now playing: {e}")
        return None


def _fetch_lastfm_now_playing(username: str) -> dict | None:
    if not LASTFM_API_KEY:
        logging.error("LASTFM_API_KEY is not set in .env; last.fm lookups are disabled.")
        return None
    try:
        url = f"https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user={urllib.parse.quote(username)}&api_key={LASTFM_API_KEY}&format=json&limit=2"
        data = _http_get_json(url, timeout=10)
        if not data or not isinstance(data, dict):
            return None
        raw_tracks = data.get("recenttracks", {}).get("track", [])
        if isinstance(raw_tracks, dict):
            raw_tracks = [raw_tracks]
        if not raw_tracks:
            return None

        track = raw_tracks[0]
        track_name = track.get("name", "")
        artist = track.get("artist", {})
        artist_name = artist.get("#text", "") if isinstance(artist, dict) else str(artist)
        if not track_name or not artist_name:
            return None

        is_now_playing = track.get("@attr", {}).get("nowplaying") == "true"
        status_text = "Currently playing" if is_now_playing else "Last played"

        cover_url = None
        images = track.get("image", [])
        if isinstance(images, list):
            for img in reversed(images):
                if isinstance(img, dict) and img.get("#text"):
                    cover_url = img["#text"]
                    break

        return {
            "track_name": track_name,
            "artist_name": artist_name,
            "status_text": status_text,
            "is_now_playing": is_now_playing,
            "spotify_url": None,
            "cover_url": cover_url,
            "service": "last.fm",
        }
    except Exception as e:
        logging.error(f"Error fetching last.fm now playing: {e}")
        return None


# --- INLINE MODE HELPERS ---
INLINE_RESULT_VERSION = "v1"

def get_inline_result_id(video_id: str) -> str:
    return hashlib.md5((video_id + INLINE_RESULT_VERSION).encode()).hexdigest()[:16]

def get_loading_markup(video_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(text="Downloading...", callback_data=f"loading:{video_id}")]])

def create_inline_audio_result(track: dict, result_id: str = None) -> InlineQueryResultAudio:
    if result_id is None:
        result_id = get_inline_result_id(track['id'])
    INLINE_SEARCH_CACHE[result_id] = track.copy()
    placeholder_url = "https://github.com/MerrcuL/music-telegram-bot/raw/refs/heads/main/placeholder.mp3"
    return InlineQueryResultAudio(
        id=result_id,
        audio_url=placeholder_url,
        title=track['title'],
        performer=track['uploader'],
        audio_duration=track.get('duration', 0),
        reply_markup=get_loading_markup(track['id']),
    )


# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        "<tg-emoji emoji-id=\"5283027211094340178\">👋</tg-emoji> Welcome to <b>MercuL's Music Bot</b>.\n\n"
        "<tg-emoji emoji-id=\"5188311512791393083\">🔎</tg-emoji> Send a song name to search.\n"
        "<tg-emoji emoji-id=\"5375129357373165375\">🔗</tg-emoji> Send a link to download directly.\n"
        "<tg-emoji emoji-id=\"5465143921912846619\">💭</tg-emoji> Use @ytmusdbot in any chat to search inline.\n\n"
        "Use /help for full instructions.",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        "<tg-emoji emoji-id=\"5472410705929971383\">📖</tg-emoji> <b>How to use this bot</b>\n\n"
        "<tg-emoji emoji-id=\"5341715473882955310\">⚙️</tg-emoji> <b>Settings & integrations</b>\n"
        "  • /settings — Configure stats.fm, last.fm, cookies, and lyrics\n"
        "  • /lucky — Toggle Feeling Lucky mode (automatically download the first search result)\n"
        "  • /now — Download your current or last played track (requires stats.fm or last.fm binding)\n"
        "  • /setcookies — Set YouTube cookies for restricted content\n\n"
        "<tg-emoji emoji-id=\"5188311512791393083\">🔎</tg-emoji> <b>Search</b>\n"
        "Send any song name and the bot will search YouTube Music and YouTube.\n"
        "Example: <i>Numb Linkin Park</i>\n"
        "  • Results are shown in pages of 5\n"
        "  • <tg-emoji emoji-id=\"5891249688933305846\">🎵</tg-emoji> = YouTube Music result\n"
        "  • The rest are all YouTube results\n"
        "  • Tap a number to download that track\n\n"
        "<tg-emoji emoji-id=\"5375129357373165375\">🔗</tg-emoji> <b>Download by link</b>\n"
        "Send a URL and the bot downloads it directly.\n"
        "Supported platforms:\n"
        "  • YouTube / YouTube Music — direct\n"
        "  • SoundCloud — direct\n"
        "  • VK Music — direct\n"
        "  • Spotify — resolved track\n\n"
        "<tg-emoji emoji-id=\"5465143921912846619\">💭</tg-emoji> <b>Inline Mode</b>\n"
        "Type @ytmusdbot in any chat to search for music.\n"
        "  • Empty query shows your currently playing / last played track (if stats.fm or last.fm connected)\n"
        "  • Type song name to search\n\n"
        "<tg-emoji emoji-id=\"5454017890736945389\">🍪</tg-emoji> <b>YouTube Cookies</b>\n"
        "Use /setcookies or the button in /settings to provide YouTube cookies.\n"
        "Cookies help bypass bot-detection and download age-restricted or region-locked content.\n"
        "  • Export cookies from your browser using an extension like <i>Get cookies.txt LOCALLY</i>\n"
        "  • The cookies must be in <b>Netscape/Mozilla format</b> (tab-separated .txt file)\n"
        "  • Only <code>.youtube.com</code> cookies are needed\n"
        "  • You can clear your cookies at any time from /settings",
        parse_mode='HTML'
    )


_USERNAME_RE = re.compile(r'[A-Za-z0-9_.\-]{2,64}')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    text = update.message.text.strip() if update.message.text else ""

    if user_id in AWAITING_COOKIES_INPUT:
        if update.message.document:
            doc = update.message.document
            if doc.file_name and doc.file_name.endswith('.txt'):
                try:
                    file = await context.bot.get_file(doc.file_id)
                    byte_array = await file.download_as_bytearray()
                    text = byte_array.decode('utf-8')
                except Exception as e:
                    await update.message.reply_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Failed to read file: {html.escape(str(e))}", parse_mode='HTML')
                    return
            else:
                await update.message.reply_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Please send a .txt file or paste the text.", parse_mode='HTML')
                return
        elif not text:
            return

        AWAITING_COOKIES_INPUT.discard(user_id)
        await _process_cookies_input(update, context, user_id, text)
        return

    if not text:
        return

    if user_id in AWAITING_STATS_FM_USERNAME:
        AWAITING_STATS_FM_USERNAME.discard(user_id)
        # A real username is one word. Anything else (a song name, a link) means the user
        # moved on, so handle the message normally instead of saving it as a username.
        if _USERNAME_RE.fullmatch(text):
            set_user_setting(user_id, "stats_fm_username", text)
            await update.message.reply_text(f"<tg-emoji emoji-id=\"6028565819225542441\">✅</tg-emoji> stats.fm username saved as <b>{html.escape(text)}</b>.", parse_mode='HTML')
            return

    if user_id in AWAITING_LASTFM_USERNAME:
        AWAITING_LASTFM_USERNAME.discard(user_id)
        # A real username is one word. Anything else (a song name, a link) means the user
        # moved on, so handle the message normally instead of saving it as a username.
        if _USERNAME_RE.fullmatch(text):
            set_user_setting(user_id, "lastfm_username", text)
            await update.message.reply_text(f"<tg-emoji emoji-id=\"6028565819225542441\">✅</tg-emoji> last.fm username saved as <b>{html.escape(text)}</b>.", parse_mode='HTML')
            return



    extracted_url = extract_url(text)
    if extracted_url:
        await handle_link(update, context, extracted_url)
    else:
        await handle_search(update, context, text)


async def _process_cookies_input(update: Update, context, user_id: int, text: str):
    """Validate, verify, and save cookies from user input."""
    if text.startswith("/"):
        await update.message.reply_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Cancelled. Use /setcookies to try again.", parse_mode='HTML')
        return

    status_msg = await update.message.reply_text("<tg-emoji emoji-id=\"5807468998840814853\">🔍</tg-emoji> Validating cookies format...", parse_mode='HTML')

    # Normalize text to handle Telegram converting tabs to spaces
    text = _normalize_cookies_text(text)

    # Step 1: Format validation
    is_valid, validation_msg = _validate_cookies_format(text)
    if not is_valid:
        await status_msg.edit_text(
            f"<tg-emoji emoji-id=\"5210952531676504517\">❌</tg-emoji> <b>Invalid cookies format</b>\n\n{validation_msg}\n\n"
            " <tg-emoji emoji-id=\"5346115921280774072\">🤔</tg-emoji> <b>Tips:</b>\n"
            "• Use a browser extension like <code>Get cookies.txt LOCALLY</code>\n"
            "• Export for <code>.youtube.com</code> domain only\n"
            "• Make sure the format is Netscape/Mozilla (tab-separated)\n"
            "• Do not copy-paste from browser DevTools (that's header format)\n\n"
            "Use /setcookies to try again.",
            parse_mode='HTML'
        )
        return

    await status_msg.edit_text(f"<tg-emoji emoji-id=\"6028565819225542441\">✅</tg-emoji> Format OK — {html.escape(str(validation_msg))}\n<tg-emoji emoji-id=\"5292226786229236118\">🔄</tg-emoji> Verifying cookies with YouTube...", parse_mode='HTML')

    # Step 2: Write temp file and verify with yt-dlp
    loop = asyncio.get_running_loop()
    cookies_path = await loop.run_in_executor(None, lambda: _write_cookies_file(user_id, text))
    works, verify_msg = await loop.run_in_executor(None, lambda: _verify_cookies_work(cookies_path))

    if not works:
        # Clean up the temp file
        await loop.run_in_executor(None, lambda: _delete_cookies_file(user_id))
        await status_msg.edit_text(
            f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> <b>Cookies verification failed</b>\n\n{html.escape(str(verify_msg))}\n\n"
            "<tg-emoji emoji-id=\"5346115921280774072\">💡</tg-emoji> <b>Tips:</b>\n"
            "• Make sure you're logged into YouTube in your browser\n"
            "• Re-export fresh cookies — they expire over time\n"
            "• Try using an incognito window, log in, then export\n\n"
            "Use /setcookies to try again.",
            parse_mode='HTML'
        )
        return

    # Step 3: Save to persistent settings
    set_user_setting(user_id, "cookies", text)
    await status_msg.edit_text(
        f"<tg-emoji emoji-id=\"6028565819225542441\">✅</tg-emoji> <b>Cookies saved successfully!</b>\n\n{html.escape(str(verify_msg))}\n\n"
        "Your cookies will now be used for all YouTube downloads.\n"
        "This helps bypass bot-detection and access restricted content.\n\n"
        "<tg-emoji emoji-id=\"5346115921280774072\">💡</tg-emoji> You can clear your cookies anytime from /settings.",
        parse_mode='HTML'
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    user_id = update.effective_user.id
    loop = asyncio.get_running_loop()
    video_id = None
    status_msg = await update.message.reply_text("<tg-emoji emoji-id=\"5375129357373165375\">🔗</tg-emoji> Processing link...", parse_mode='HTML')

    try:
        url_type = get_url_type(url)
        download_url = url
        resolved_title = resolved_artist = resolved_cover = None

        if url_type == 'spotify':
            await status_msg.edit_text("<tg-emoji emoji-id=\"5807468998840814853\">🔍</tg-emoji> Resolving Spotify track...", parse_mode='HTML')
            sp_info = await loop.run_in_executor(None, lambda: resolve_spotify_track(url))
            if sp_info:
                resolved_title = sp_info.get("title")
                resolved_artist = sp_info.get("artist") or "Unknown Artist"
                resolved_cover = sp_info.get("cover_url")
                if is_too_long(sp_info.get("duration")):
                    await status_msg.edit_text(TrackTooLongError(sp_info.get("duration")).user_text, parse_mode='HTML')
                    return

            if resolved_title:
                await status_msg.edit_text(
                    f"<tg-emoji emoji-id=\"5807468998840814853\">🔍</tg-emoji> Searching YouTube for: <b>{html.escape(resolved_title)}</b> — {html.escape(resolved_artist)}...",
                    parse_mode='HTML'
                )
                search_res = await loop.run_in_executor(
                    None, lambda: search_spotify_track_on_youtube(resolved_artist, resolved_title, spotify_url=url)
                )
                if search_res:
                    download_url = f"https://www.youtube.com/watch?v={search_res['id']}"
                    video_id = search_res['id']
                else:
                    logging.warning(
                        f"YouTube search failed for Spotify track: "
                        f"artist={resolved_artist!r}, title={resolved_title!r}, url={url!r}"
                    )
                    await status_msg.edit_text(
                        f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Resolved <b>{html.escape(resolved_title)}</b> — {html.escape(resolved_artist)} from Spotify, "
                        "but could not find a matching track on YouTube.\n"
                        "Try searching for the track name directly.",
                        parse_mode='HTML'
                    )
                    return
            else:
                logging.warning(f"Failed to resolve Spotify track for url: {url}")
                await status_msg.edit_text(
                    "<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Could not resolve this Spotify link.\n"
                    "Please make sure the track exists or try searching by name.",
                    parse_mode='HTML'
                )
                return

            await status_msg.edit_text(
                f"<tg-emoji emoji-id=\"6028565819225542441\">✅</tg-emoji> Resolved: <b>{html.escape(resolved_title)}</b> — {html.escape(resolved_artist)}\n<tg-emoji emoji-id=\"5298853345241358103\">💾</tg-emoji> Checking cache...",
                parse_mode='HTML'
            )

        # Fast-path: extract video ID via regex for YouTube URLs
        yt_match = re.search(r'(?:v=|/)([0-9A-Za-z_-]{11}).*', download_url)
        if yt_match and ('youtube.com' in download_url or 'youtu.be' in download_url):
            video_id = yt_match.group(1)
            info_only = None
        else:
            try:
                info_only = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: get_url_info(download_url, user_id)),
                    timeout=30
                )
                video_id = info_only.get('id', '')
            except Exception as e:
                logging.warning(f"Info-only extraction failed (will still try download): {e}")
                info_only = None

        if video_id:
            cached_audio = await get_cached_audio(video_id)
            if cached_audio and cached_audio.tg_file_id:
                # Apply metadata cleaning even for cached path
                if resolved_title:
                    song_title, uploader = resolved_title, resolved_artist or 'Unknown Artist'
                elif info_only:
                    song_title, uploader = yt_metadata(info_only)
                else:
                    song_title = cached_audio.title or 'Unknown Track'
                    uploader = cached_audio.performer or 'Unknown Artist'
                duration = (info_only.get('duration') if info_only else None) or cached_audio.duration
                success = await send_downloaded_audio(
                    update, context, user_id, video_id,
                    song_title, uploader, duration,
                    original_url=url, title_hint=f"{uploader} {song_title}",
                    cover_url=resolved_cover,
                )
                if success:
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: could not send cached audio.", parse_mode='HTML')
                return

        # resolved_title/artist came from Spotify resolution — authoritative
        await process_download_and_upload(
            update, context, user_id, video_id,
            status_msg=status_msg,
            fallback_title=resolved_title, fallback_artist=resolved_artist,
            original_url=url,
            download_url=download_url,
            authoritative=bool(resolved_title),
            cover_url=resolved_cover,
        )

    except Exception as e:
        logging.error(f"Link Download Error: {e}")
        await status_msg.edit_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: {html.escape(str(e))}", parse_mode='HTML')


async def _download_and_send_result_track(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    track: dict,
    status_msg,
):
    video_id = track.get('id')
    if not video_id:
        await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: invalid track.", parse_mode='HTML')
        return

    cached_audio = await get_cached_audio(video_id)
    if cached_audio and cached_audio.tg_file_id:
        song_title = track.get('title') or (cached_audio.title or 'Unknown Track')
        uploader = track.get('uploader') or (cached_audio.performer or 'Unknown Artist')
        duration = cached_audio.duration or track.get('duration')
        try:
            success = await send_downloaded_audio(
                update_or_query, context, user_id, video_id,
                song_title, uploader, duration,
                original_url=track.get('spotify_url'),
                title_hint=f"{uploader} {song_title}",
                cover_url=track.get('cover_url'),
            )
            if success:
                await status_msg.delete()
            else:
                await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: could not send cached audio.", parse_mode='HTML')
        except Exception as e:
            logging.error(f"Cache send error: {e}")
            await status_msg.edit_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: {html.escape(str(e))}", parse_mode='HTML')
        return

    if is_too_long(track.get('duration')):
        await status_msg.edit_text(TrackTooLongError(track.get('duration')).user_text, parse_mode='HTML')
        return

    _fallback_title = track.get('title') if track.get('source') == '🎵' else None
    _fallback_artist = track.get('uploader') if track.get('source') == '🎵' else None
    _fallback_cover = track.get('cover_url')

    try:
        await process_download_and_upload(
            update_or_query, context, user_id, video_id,
            status_msg=status_msg,
            fallback_title=_fallback_title, fallback_artist=_fallback_artist,
            download_url=f"https://www.youtube.com/watch?v={video_id}",
            cover_url=_fallback_cover,
        )
    except Exception as e:
        logging.error(f"Download Error: {e}")
        await status_msg.edit_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: {html.escape(str(e))}", parse_mode='HTML')


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(f"<tg-emoji emoji-id=\"5807468998840814853\">🔎</tg-emoji> Searching <b>{html.escape(query)}</b>...", parse_mode='HTML')

    try:
        results = await search_hybrid(query, user_id)
        if not results:
            await status_msg.edit_text("No results found.")
            return

        if get_user_setting(user_id, "feeling_lucky", False):
            await _download_and_send_result_track(update, context, user_id, results[0], status_msg)
            return

        SEARCH_CACHE[user_id] = results
        total_pages = (len(results) + 4) // 5
        text, reply_markup = build_display(results, 0, total_pages)
        await status_msg.edit_text(text, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Search Error: {e}")
        await status_msg.edit_text("An error occurred.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await query.answer()
        return

    data = query.data or ""

    # Telegram shows only the first answer to a button press, so every branch answers once:
    # settings actions with their own notice, everything else with an empty one.
    if not data.startswith("settings:"):
        await query.answer()
    else:
        action = data.split(":")[1]
        if action == "bind_stats_fm":
            AWAITING_STATS_FM_USERNAME.add(user_id)
            await query.answer("💬 Please send your stats.fm username")
            await query.message.reply_text("<tg-emoji emoji-id=\"5350719228573954225\">💬</tg-emoji> Please send your stats.fm username (make sure your profile is public):", parse_mode='HTML')
            return
        elif action == "unbind_stats_fm":
            AWAITING_STATS_FM_USERNAME.discard(user_id)
            set_user_setting(user_id, "stats_fm_username", None)
            await query.answer("stats.fm account unbound ✅")
            await query.edit_message_text(
                _build_settings_text(user_id),
                reply_markup=_build_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "bind_lastfm":
            AWAITING_LASTFM_USERNAME.add(user_id)
            await query.answer("💬 Please send your last.fm username")
            await query.message.reply_text("<tg-emoji emoji-id=\"5350719228573954225\">💬</tg-emoji> Please send your last.fm username:", parse_mode='HTML')
            return
        elif action == "unbind_lastfm":
            AWAITING_LASTFM_USERNAME.discard(user_id)
            set_user_setting(user_id, "lastfm_username", None)
            await query.answer("last.fm account unbound ✅")
            await query.edit_message_text(
                _build_settings_text(user_id),
                reply_markup=_build_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "toggle_lyrics":
            new_val = not get_user_setting(user_id, "include_lyrics", False)
            set_user_setting(user_id, "include_lyrics", new_val)
            await query.answer(f"Lyrics {'enabled ✅' if new_val else 'disabled ❌'}")
            await query.edit_message_text(
                _build_settings_text(user_id),
                reply_markup=_build_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "toggle_lucky":
            new_val = not get_user_setting(user_id, "feeling_lucky", False)
            set_user_setting(user_id, "feeling_lucky", new_val)
            toast_text = (
                "🍀 Feeling lucky: ON\nAutomatically downloads the first result without showing the list."
                if new_val else
                "🍀 Feeling lucky: OFF\nShows the list of search results to choose from."
            )
            await query.answer(toast_text, show_alert=False)
            await query.edit_message_text(
                _build_settings_text(user_id),
                reply_markup=_build_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "set_cookies":
            AWAITING_COOKIES_INPUT.add(user_id)
            await query.answer("🍪 Please send your cookies")
            await query.message.reply_text(
                "<tg-emoji emoji-id=\"5195026981921510830\">🍪</tg-emoji> <b>Set YouTube Cookies</b>\n\n"
                "Please paste your cookies in <b>Netscape/Mozilla format</b> below.\n\n"
                "<tg-emoji emoji-id=\"5346115921280774072\">💡</tg-emoji> <b>How to export cookies:</b>\n"
                "1. Install a browser extension like <code>Get cookies.txt LOCALLY</code>\n"
                "2. Go to <code>youtube.com</code> and make sure you're logged in\n"
                "3. Click the extension and export cookies for this site\n"
                "4. Open the downloaded <code>.txt</code> file and copy all contents\n"
                "5. Paste the text here\n\n"
                "<tg-emoji emoji-id=\"5255949705740843980\">⚠️</tg-emoji> The text should contain tab-separated lines, <b>not</b> JSON or HTTP headers.",
                parse_mode='HTML'
            )
            return
        elif action == "clear_cookies":
            set_user_setting(user_id, "cookies", None)
            _delete_cookies_file(user_id)
            await query.answer("🍪 Cookies cleared ✅")
            await query.edit_message_text(
                _build_settings_text(user_id),
                reply_markup=_build_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "menu_links":
            await query.answer()
            await query.edit_message_text(
                _build_links_settings_text(user_id),
                reply_markup=_build_links_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "menu_main":
            await query.answer()
            await query.edit_message_text(
                _build_settings_text(user_id),
                reply_markup=_build_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "toggle_link_yt":
            new_val = not get_user_setting(user_id, "link_youtube", False)
            set_user_setting(user_id, "link_youtube", new_val)
            await query.answer(f"YouTube links {'ON' if new_val else 'OFF'}")
            await query.edit_message_text(
                _build_links_settings_text(user_id),
                reply_markup=_build_links_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "toggle_link_sp":
            new_val = not get_user_setting(user_id, "link_spotify", False)
            set_user_setting(user_id, "link_spotify", new_val)
            await query.answer(f"Spotify links {'ON' if new_val else 'OFF'}")
            await query.edit_message_text(
                _build_links_settings_text(user_id),
                reply_markup=_build_links_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "toggle_link_am":
            new_val = not get_user_setting(user_id, "link_apple", False)
            set_user_setting(user_id, "link_apple", new_val)
            await query.answer(f"Apple links {'ON' if new_val else 'OFF'}")
            await query.edit_message_text(
                _build_links_settings_text(user_id),
                reply_markup=_build_links_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        elif action == "toggle_link_ya":
            new_val = not get_user_setting(user_id, "link_yandex", False)
            set_user_setting(user_id, "link_yandex", new_val)
            await query.answer(f"Yandex links {'ON' if new_val else 'OFF'}")
            await query.edit_message_text(
                _build_links_settings_text(user_id),
                reply_markup=_build_links_settings_markup(user_id),
                parse_mode='HTML'
            )
            return
        await query.answer()
        return

    if data.startswith("page:"):
        new_page = int(data.split(":")[1])
        results = SEARCH_CACHE.get(user_id)
        if not results:
            await query.edit_message_text("<tg-emoji emoji-id=\"5210952531676504517\">❌</tg-emoji> Search expired. Please search again.", parse_mode='HTML')
            return
        total_pages = (len(results) + 4) // 5
        text, reply_markup = build_display(results, new_page, total_pages)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
        return

    if data == "cancel":
        await query.message.delete()
        SEARCH_CACHE.pop(user_id, None)
        return

    if data == "noop":
        return

    if data.startswith("dl:"):
        video_id = data.split(":")[1]
        results_cache = SEARCH_CACHE.get(user_id, [])
        cached_track = next((r for r in results_cache if r['id'] == video_id), None)
        if not cached_track:
            cached_track = {'id': video_id}
        await _download_and_send_result_track(update, context, user_id, cached_track, query.message)
        return


def _build_settings_text(user_id: int) -> str:
    """Build the settings message text for a user."""
    username = get_user_setting(user_id, "stats_fm_username") or "None"
    lastfm_username = get_user_setting(user_id, "lastfm_username") or "None"
    has_cookies = bool(get_user_setting(user_id, "cookies"))
    cookies_status = "Set" if has_cookies else "Not set"
    lyrics_on = get_user_setting(user_id, "include_lyrics", False)
    return (
        f"<tg-emoji emoji-id=\"5341715473882955310\">⚙️</tg-emoji> <b>Settings</b>\n\n"
        f"stats.fm username: <code>{html.escape(str(username))}</code>\n"
        f"last.fm username: <code>{html.escape(str(lastfm_username))}</code>\n"
        f"YouTube cookies: <code>{cookies_status}</code>\n"
    )


def _build_settings_markup(user_id: int) -> InlineKeyboardMarkup:
    """Build the settings inline keyboard for a user."""
    lyrics_on = get_user_setting(user_id, "include_lyrics", False)
    lucky_on = get_user_setting(user_id, "feeling_lucky", False)
    has_cookies = bool(get_user_setting(user_id, "cookies"))

    stats_user = get_user_setting(user_id, "stats_fm_username")
    has_statsfm = bool(stats_user and stats_user != "None")
    if has_statsfm:
        statsfm_btn = InlineKeyboardButton("Unbind stats.fm account", callback_data="settings:unbind_stats_fm", icon_custom_emoji_id="5350347232866484976")
    else:
        statsfm_btn = InlineKeyboardButton("Bind stats.fm account", callback_data="settings:bind_stats_fm", icon_custom_emoji_id="5350347232866484976")

    lastfm_user = get_user_setting(user_id, "lastfm_username")
    has_lastfm = bool(lastfm_user and lastfm_user != "None")
    if has_lastfm:
        lastfm_btn = InlineKeyboardButton("Unbind last.fm account", callback_data="settings:unbind_lastfm", icon_custom_emoji_id=LASTFM_ICON_EMOJI_ID)
    else:
        lastfm_btn = InlineKeyboardButton("Bind last.fm account", callback_data="settings:bind_lastfm", icon_custom_emoji_id=LASTFM_ICON_EMOJI_ID)

    if has_cookies:
        cookies_btn = InlineKeyboardButton("Clear cookies", callback_data="settings:clear_cookies", icon_custom_emoji_id="5386830480375050148")
    else:
        cookies_btn = InlineKeyboardButton("Set cookies", callback_data="settings:set_cookies", icon_custom_emoji_id="5386830480375050148")

    return InlineKeyboardMarkup([
        [statsfm_btn],
        [lastfm_btn],
        [InlineKeyboardButton(f"Include lyrics: {'ON' if lyrics_on else 'OFF'}", callback_data="settings:toggle_lyrics", icon_custom_emoji_id="5886506997066502636")],
        [InlineKeyboardButton(
            f"Feeling lucky: {'ON' if lucky_on else 'OFF'}",
            callback_data="settings:toggle_lucky",
            icon_custom_emoji_id="5233619281710692734" if lucky_on else "5766994197705921104"
        )],
        [cookies_btn],
        [InlineKeyboardButton("Links", callback_data="settings:menu_links", icon_custom_emoji_id="6028171274939797252")],
    ])


def _build_links_settings_text(user_id: int) -> str:
    """Build the settings message text for the links submenu."""
    return (
        f"<tg-emoji emoji-id=\"5341715473882955310\">⚙️</tg-emoji> <b>Links Settings</b>\n\n"
    )


def _build_links_settings_markup(user_id: int) -> InlineKeyboardMarkup:
    """Build the settings inline keyboard for the links submenu."""
    yt_on = get_user_setting(user_id, "link_youtube", False)
    sp_on = get_user_setting(user_id, "link_spotify", False)
    am_on = get_user_setting(user_id, "link_apple", False)
    ya_on = get_user_setting(user_id, "link_yandex", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"YouTube: {'ON' if yt_on else 'OFF'}", callback_data="settings:toggle_link_yt", icon_custom_emoji_id="5302999436085962212")],
        [InlineKeyboardButton(f"Spotify: {'ON' if sp_on else 'OFF'}", callback_data="settings:toggle_link_sp", icon_custom_emoji_id="5303475095124062081")],
        [InlineKeyboardButton(f"Apple: {'ON' if am_on else 'OFF'}", callback_data="settings:toggle_link_am", icon_custom_emoji_id="5298597854816780205")],
        [InlineKeyboardButton(f"Yandex: {'ON' if ya_on else 'OFF'}", callback_data="settings:toggle_link_ya", icon_custom_emoji_id=YANDEX_ICON_EMOJI_ID)],
        [InlineKeyboardButton("Back", callback_data="settings:menu_main", icon_custom_emoji_id="5807679830195444280", style="primary")]
    ])



async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        _build_settings_text(user_id),
        reply_markup=_build_settings_markup(user_id),
        parse_mode='HTML'
    )


async def lucky_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /lucky command — toggle Feeling Lucky mode."""
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return
    new_val = not get_user_setting(user_id, "feeling_lucky", False)
    set_user_setting(user_id, "feeling_lucky", new_val)
    if new_val:
        await update.message.reply_text(
            "<tg-emoji emoji-id=\"5258040062028822951\">🍀</tg-emoji> <b>Feeling lucky: ON</b>\n\n"
            "The bot will automatically download the first YouTube Music result without showing a list.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "<tg-emoji emoji-id=\"5258040062028822951\">🍀</tg-emoji> <b>Feeling lucky: OFF</b>\n\n"
            "The bot will show a list of search results to choose from.",
            parse_mode='HTML'
        )


async def setcookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setcookies command — prompt user for cookies input."""
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    has_cookies = bool(get_user_setting(user_id, "cookies"))
    if has_cookies:
        await update.message.reply_text(
            "<tg-emoji emoji-id=\"5195026981921510830\">🍪</tg-emoji> <b>You already have cookies set.</b>\n\n"
            "Sending new cookies will replace the existing ones.\n"
            "To clear cookies instead, use /settings.\n\n"
            "Paste your new cookies in <b>Netscape/Mozilla format</b> below, "
            "or send /cancel to abort.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "<tg-emoji emoji-id=\"5195026981921510830\">🍪</tg-emoji> <b>Set YouTube Cookies</b>\n\n"
            "Cookies allow the bot to bypass YouTube bot-detection and "
            "download age-restricted or region-locked content.\n\n"
            " <tg-emoji emoji-id=\"5346115921280774072\">🤔</tg-emoji> <b>How to export cookies:</b>\n"
            "1. Install a browser extension like <i>Get cookies.txt LOCALLY</i>\n"
            "2. Go to <code>youtube.com</code> and make sure you're logged in\n"
            "3. Click the extension and export cookies for this site\n"
            "4. Copy file contents or just send the file.",
            parse_mode='HTML'
        )
    AWAITING_COOKIES_INPUT.add(user_id)


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    stats_username = get_user_setting(user_id, "stats_fm_username")
    lastfm_username = get_user_setting(user_id, "lastfm_username")

    has_statsfm = bool(stats_username and stats_username != "None")
    has_lastfm = bool(lastfm_username and lastfm_username != "None")

    if not has_statsfm and not has_lastfm:
        await update.message.reply_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> You haven't bound your stats.fm or last.fm account yet. Use /settings to bind it.", parse_mode='HTML')
        return

    service_pref = None
    if context.args:
        arg = context.args[0].lower()
        if arg in ("lastfm", "last.fm", "lfm"):
            if not has_lastfm:
                await update.message.reply_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> You haven't bound your last.fm account yet. Use /settings to bind it.", parse_mode='HTML')
                return
            service_pref = "lastfm"
        elif arg in ("statsfm", "stats.fm", "sfm"):
            if not has_statsfm:
                await update.message.reply_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> You haven't bound your stats.fm account yet. Use /settings to bind it.", parse_mode='HTML')
                return
            service_pref = "statsfm"

    status_msg = await update.message.reply_text("<tg-emoji emoji-id=\"5350326599843621291\">🎧</tg-emoji> Fetching current track...", parse_mode='HTML')
    video_id = None

    try:
        loop = asyncio.get_running_loop()

        def _fetch():
            if service_pref == "lastfm":
                return _fetch_lastfm_now_playing(lastfm_username)
            if service_pref == "statsfm":
                return _fetch_statsfm_now_playing(stats_username)
            if has_statsfm and not has_lastfm:
                return _fetch_statsfm_now_playing(stats_username)
            if has_lastfm and not has_statsfm:
                return _fetch_lastfm_now_playing(lastfm_username)

            # Both configured: prefer currently playing
            sf_res = _fetch_statsfm_now_playing(stats_username)
            if sf_res and sf_res.get("is_now_playing"):
                return sf_res
            lfm_res = _fetch_lastfm_now_playing(lastfm_username)
            if lfm_res and lfm_res.get("is_now_playing"):
                return lfm_res
            return sf_res or lfm_res

        track_data = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=20)

        if not track_data or not track_data.get("track_name"):
            await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> No recent tracks found or profile is private.", parse_mode='HTML')
            return

        track_name = track_data["track_name"]
        artist_name = track_data.get("artist_name", "")
        status_text = track_data.get("status_text", "Currently playing")
        spotify_url = track_data.get("spotify_url")
        cover_url = track_data.get("cover_url")
        service_label = track_data.get("service", "")

        search_query = f"{artist_name} {track_name}".strip()
        if not search_query:
            await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Could not extract track information.", parse_mode='HTML')
            return

        service_suffix = f" ({service_label})" if (has_statsfm and has_lastfm) else ""
        await status_msg.edit_text(
            f"<tg-emoji emoji-id=\"5258396243666681152\">🔎</tg-emoji> {status_text} track{service_suffix}:\n<b>{html.escape(artist_name)} - {html.escape(track_name)}</b>",
            parse_mode='HTML'
        )

        best_result = await loop.run_in_executor(
            None, lambda: search_spotify_track_on_youtube(artist_name, track_name, spotify_url)
        )

        if not best_result:
            results = await search_hybrid(search_query, user_id)
            if not results:
                await status_msg.edit_text(
                    f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Could not find <b>{html.escape(artist_name)} - {html.escape(track_name)}</b> on YouTube.",
                    parse_mode='HTML'
                )
                return
            best_result = results[0]

        video_id = best_result['id']
        effective_cover_url = best_result.get('cover_url') or cover_url

        if is_too_long(best_result.get('duration')):
            await status_msg.edit_text(TrackTooLongError(best_result.get('duration')).user_text, parse_mode='HTML')
            return

        cached_audio = await get_cached_audio(video_id)
        if cached_audio and cached_audio.tg_file_id:
            success = await send_downloaded_audio(
                update, context, user_id, video_id,
                track_name or best_result.get('title', 'Unknown Track'),
                artist_name or best_result.get('uploader', 'Unknown Artist'),
                best_result.get('duration'),
                original_url=spotify_url, title_hint=f"{artist_name} {track_name}",
                cover_url=effective_cover_url,
            )
            if success:
                await status_msg.delete()
            else:
                await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: could not send cached audio.", parse_mode='HTML')
            return

        # Authoritative metadata from stats.fm / last.fm — always use it over yt-dlp title guessing
        await process_download_and_upload(
            update, context, user_id, video_id,
            status_msg=status_msg,
            fallback_title=track_name, fallback_artist=artist_name,
            original_url=spotify_url,
            download_url=f"https://www.youtube.com/watch?v={video_id}",
            authoritative=True,
            cover_url=effective_cover_url,
        )

    except Exception as e:
        logging.error(f"Now Command Error: {e}")
        await status_msg.edit_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Error: {html.escape(str(e))}", parse_mode='HTML')


# --- INLINE MODE HANDLERS ---

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.inline_query.answer([], cache_time=0)
        return

    query = update.inline_query.query.strip()
    results = []

    # Telegram sends a query for every pause in typing. Wait a moment and only answer the
    # newest one, so half-typed queries do not each trigger a full search.
    _latest_inline_query[user_id] = update.inline_query.id
    if query:
        if len(query) < 2:
            await update.inline_query.answer([], cache_time=0, is_personal=True)
            return
        if " ".join(query.lower().split()) not in HYBRID_SEARCH_CACHE:
            await asyncio.sleep(0.4)
            if _latest_inline_query.get(user_id) != update.inline_query.id:
                return

    if not query:
        statsfm_tracks, lastfm_tracks = await asyncio.gather(
            get_statsfm_tracks_info(user_id),
            get_lastfm_tracks_info(user_id),
        )

        combined_tracks = []
        seen = set()
        for track_info in statsfm_tracks + lastfm_tracks:
            key = (track_info['artist_name'].lower(), track_info['track_name'].lower())
            if key not in seen:
                seen.add(key)
                combined_tracks.append(track_info)

        if combined_tracks:
            for info in combined_tracks:
                prefix = "lastfm" if info.get("has_lastfm") else "statsfm"
                result_id = f"{prefix}:{hashlib.md5((info['artist_name'] + info['track_name']).encode()).hexdigest()[:16]}"
                INLINE_SEARCH_CACHE[result_id] = info
                placeholder_url = "https://github.com/MerrcuL/music-telegram-bot/raw/refs/heads/main/placeholder.mp3"

                results.append(InlineQueryResultAudio(
                    id=result_id,
                    audio_url=placeholder_url,
                    title=f"{info['status_text']}: {info['track_name']}",
                    performer=info['artist_name'],
                    reply_markup=get_loading_markup(result_id)
                ))
        else:
            from telegram import InlineQueryResultArticle, InputTextMessageContent
            results.append(InlineQueryResultArticle(
                id="help",
                title="Type a song name to search",
                description="Connect stats.fm or last.fm in /settings to see your currently playing track here",
                input_message_content=InputTextMessageContent(
                    "<tg-emoji emoji-id=\"5465143921912846619\">💬</tg-emoji> <b>Inline Music Search</b>\n\n"
                    "Type @ytmusdbot followed by a song name to search.\n"
                    "Connect your stats.fm or last.fm account in /settings to see your currently playing track here!",
                    parse_mode='HTML'
                )
            ))
    else:
        try:
            search_results = await search_hybrid(query, user_id)
            for track in search_results[:20]:
                results.append(create_inline_audio_result(track))
        except Exception as e:
            logging.error(f"Inline search error: {e}")

    if _latest_inline_query.get(user_id) == update.inline_query.id:
        _latest_inline_query.pop(user_id, None)
    try:
        await update.inline_query.answer(results, cache_time=0, is_personal=True)
    except Exception as e:
        # Usually "query is too old": the user kept typing and a newer query took over.
        logging.debug(f"Could not answer inline query: {e}")


async def chosen_inline_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chosen = update.chosen_inline_result
    user_id = chosen.from_user.id
    result_id = chosen.result_id
    inline_message_id = chosen.inline_message_id

    logging.info(f"chosen_inline_result_handler triggered: result_id={result_id}, user_id={user_id}, inline_message_id={inline_message_id}")

    def _make_status_markup(text: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(text=text, callback_data="noop")]])

    def _make_error_markup(text: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton(text=text, callback_data="noop", icon_custom_emoji_id="5348027250446967673")]])

    async def _update_status(text: str):
        try:
            await context.bot.edit_message_reply_markup(
                inline_message_id=inline_message_id,
                reply_markup=_make_status_markup(text),
            )
        except Exception as e:
            logging.warning(f"Could not update inline status to '{text}': {e}")

    async def _show_error(text: str):
        try:
            await context.bot.edit_message_reply_markup(
                inline_message_id=inline_message_id,
                reply_markup=_make_error_markup(text),
            )
        except Exception as e:
            logging.warning(f"Could not show inline error '{text}': {e}")

    if user_id not in ALLOWED_USERS:
        logging.warning(f"User {user_id} not in allowed list")
        return
    if not inline_message_id:
        logging.warning("No inline_message_id available, cannot edit message")
        return
    track = INLINE_SEARCH_CACHE.get(result_id)
    if track is None:
        # Bot was restarted or the result is over an hour old: say so instead of
        # leaving the message on "Downloading..." forever.
        logging.warning(f"Result ID {result_id} not found in cache")
        await _show_error("Expired, please search again")
        return

    if result_id.startswith("statsfm:") or result_id.startswith("lastfm:"):
        source_name = "last.fm" if result_id.startswith("lastfm:") else "stats.fm"
        deferred_info = track
        logging.info(f"Deferred YouTube search for {source_name} track: {deferred_info['track_name']}")
        await _update_status("Searching...")
        loop = asyncio.get_running_loop()
        try:
            best_result = await loop.run_in_executor(
                None,
                lambda: search_spotify_track_on_youtube(
                    deferred_info["artist_name"],
                    deferred_info["track_name"],
                    deferred_info.get("spotify_url")
                )
            )
            if not best_result:
                search_results = await search_hybrid(deferred_info["search_query"], user_id)
                best_result = search_results[0] if search_results else None

            if not best_result:
                logging.error(f"Could not find deferred {source_name} track on YouTube.")
                await _show_error("Track not found on YouTube")
                return

            track = best_result.copy()
            if deferred_info.get("spotify_url"):
                track['spotify_url'] = deferred_info['spotify_url']
            if deferred_info.get("artist_name"):
                track['uploader'] = deferred_info['artist_name']
            if deferred_info.get("track_name"):
                track['title'] = deferred_info['track_name']
            if deferred_info.get("cover_url") and not track.get("cover_url"):
                track['cover_url'] = deferred_info['cover_url']

        except Exception as e:
            logging.error(f"Error in deferred {source_name} search: {e}")
            await _show_error("Search failed")
            return

    video_id = track['id']
    logging.info(f"Processing chosen inline result: result_id={result_id}, video_id={video_id}, user_id={user_id}")

    # One download per video at a time; a second request waits and then finds it in the cache.
    async with video_lock(video_id):
        cached = await get_cached_audio(video_id)
        tg_file_id = None

        try:
            if cached and cached.tg_file_id:
                logging.info(f"Using cached audio for video_id: {video_id}")
                tg_file_id = cached.tg_file_id
                if cached.title and not (result_id.startswith("statsfm:") or result_id.startswith("lastfm:")):
                    track['title'] = cached.title
                    track['uploader'] = cached.performer or track.get('uploader')
            else:
                if not DUMP_CHAT_ID:
                    logging.error("DUMP_CHAT_ID not configured, cannot cache audio")
                    await _show_error("Bot misconfigured")
                    return
                if is_too_long(track.get('duration')):
                    await _show_error(TrackTooLongError(track.get('duration')).short_text)
                    return

                await _update_status("Downloading...")
                logging.info(f"Downloading and caching audio for video_id: {video_id}")
                loop = asyncio.get_running_loop()

                has_cookies = bool(_ensure_cookies_file(user_id)) if user_id else False

                include_lyrics = get_user_setting(user_id, "include_lyrics", False)
                meta_future = loop.run_in_executor(
                    None, lambda: _fetch_metadata_bundle(
                        video_id=video_id,
                        artist=track.get('uploader'),
                        track=track.get('title'),
                        original_url=track.get('spotify_url'),
                        include_lyrics=include_lyrics,
                    )
                )

                # Cookies first when they are set (same as regular mode).
                info = None
                try:
                    info = await download_preferring_cookies(
                        user_id, video_id, has_cookies=has_cookies,
                        on_fallback=lambda: _update_status("Retrying without cookies..."),
                    )
                except DownloadRejected as rejected:
                    logging.info(f"Inline: download refused for {video_id}: {rejected}")
                    await _show_error(rejected.short_text)
                    return
                except asyncio.TimeoutError:
                    logging.error(f"Inline: download timed out for video_id: {video_id}")
                    await _show_error("Download timed out")
                    return
                except Exception as download_err:
                    logging.error(f"Inline: download failed for video_id {video_id}: {download_err}")
                    known = describe_download_error(download_err, has_cookies)
                    await _show_error(known[1] if known else "Download failed")
                    return

                song_title, uploader = yt_metadata(info)
                duration = info.get('duration', track.get('duration', 0))
                logging.info(f"Downloaded: {song_title} by {uploader}, duration: {duration}")

                actual_file = await find_output_file(DOWNLOAD_DIR, video_id)
                if not actual_file:
                    logging.error(f"Downloaded file not found for video_id: {video_id}")
                    await _show_error("File not found after download")
                    return

                meta_bundle = {}
                if meta_future is not None:
                    try:
                        meta_bundle = await asyncio.wait_for(asyncio.shield(meta_future), timeout=4)
                    except Exception:
                        pass
                genius_data = meta_bundle.get('genius', {})
                lyrics = meta_bundle.get('lyrics')
                embed_lyrics = meta_bundle.get('embed_lyrics')

                # Get synchronized artwork pair (embed_cover_bytes for MP3 ID3, tg_thumb_bytes for Telegram message)
                embed_bytes, tg_thumb_bytes = await _get_artwork_pair(
                    video_id, artist=uploader, track=song_title,
                    cover_url=track.get('cover_url'),
                    genius_cover_url=genius_data.get('cover_url'),
                    genius_checked=bool(genius_data.get('matched')),
                    thumbnail_url=info.get('thumbnail'),
                )

                # Embed clean ID3v2.3 tags and album art into MP3 file (always uses LRCLIB for lyrics)
                await asyncio.to_thread(
                    _prepare_and_embed_metadata,
                    actual_file,
                    title=song_title,
                    artist=uploader,
                    cover_bytes=embed_bytes,
                    album=genius_data.get('album'),
                    album_artist=genius_data.get('artist') or uploader,
                    release_date=genius_data.get('release_date'),
                    lyrics=embed_lyrics or lyrics,
                    composer=genius_data.get('composer'),
                    lyricist=genius_data.get('lyricist'),
                )

                thumb_file = None
                if tg_thumb_bytes:
                    thumb_file = io.BytesIO(tg_thumb_bytes)
                    thumb_file.name = "thumbnail.jpg"

                await _update_status("Uploading...")
                audio_filename = format_audio_filename(uploader, song_title, ext=os.path.splitext(actual_file)[1])
                try:
                    with open(actual_file, 'rb') as audio_file:
                        sent_msg = await context.bot.send_audio(
                            chat_id=DUMP_CHAT_ID, audio=audio_file,
                            title=song_title, performer=uploader, duration=duration,
                            thumbnail=thumb_file,
                            filename=audio_filename,
                            read_timeout=120, write_timeout=120,
                        )
                        if not sent_msg.audio:
                            raise RuntimeError("Telegram did not store the upload as audio")
                        tg_file_id = sent_msg.audio.file_id
                        logging.info(f"Uploaded to dump chat, got file_id: {tg_file_id}")

                    await save_cached_audio(video_id, tg_file_id, song_title, uploader, duration)
                    logging.info(f"Saved to cache: video_id={video_id}")

                except Exception as e:
                    logging.error(f"Failed to upload to dump chat: {e}")
                    await _show_error("Upload failed")
                    return
                finally:
                    await cleanup_files(video_id)

            if not tg_file_id:
                logging.error("No tg_file_id available, cannot edit message")
                await _show_error("No file to send")
                return

            caption = None
            include_lyrics = get_user_setting(user_id, "include_lyrics", False)

            if include_lyrics and 'lyrics' in locals() and lyrics:
                clean_art = clean_genius_title(track['uploader'])
                clean_tit = clean_genius_title(track['title'])
                escaped_art = html.escape(clean_art)
                escaped_tit = html.escape(clean_tit)
                header = f"<b>{escaped_art} — {escaped_tit}</b>\n\n"
                lyrics_block = f"<blockquote expandable>{html.escape(lyrics.strip())}</blockquote>"
                candidate_caption = f"{caption}\n\n{lyrics_block}".strip() if caption else f"{header}{lyrics_block}"
                if len(candidate_caption) <= 1024:
                    caption = candidate_caption


            logging.info(f"Editing inline message {inline_message_id} with file_id {tg_file_id}")
            links_markup = await build_track_links_markup(
                user_id=user_id,
                video_id=video_id,
                artist=track.get('uploader'),
                track=track.get('title'),
                original_url=track.get('spotify_url'),
                metadata=genius_data if 'genius_data' in locals() else None,
                is_inline=True,
            )
            try:
                await context.bot.edit_message_media(
                    media=InputMediaAudio(
                        media=tg_file_id,
                        title=track['title'],
                        performer=track['uploader'],
                        duration=track.get('duration', 0),
                        filename=format_audio_filename(track.get('uploader'), track.get('title')),
                    ),
                    inline_message_id=inline_message_id,
                    reply_markup=links_markup,
                )
                if caption:
                    await context.bot.edit_message_caption(
                        inline_message_id=inline_message_id,
                        caption=caption, parse_mode='HTML',
                        reply_markup=links_markup,
                    )
            except Exception as e:
                logging.error(f"Failed to edit message media: {e}")
                await _show_error("Failed to update message")

        except Exception as e:
            logging.error(f"Error handling chosen inline result: {e}", exc_info=True)
            await _show_error("Unexpected error")
        finally:
            # Ensure cleanup even if the outer try fails
            await cleanup_files(video_id)


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Debug yt-dlp format availability for a given video."""
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: <code>/debug VIDEO_ID_OR_URL</code>\n"
            "Example: <code>/debug _ewS1PjD1mE</code>",
            parse_mode='HTML'
        )
        return

    target = args[0].strip()
    # Extract video ID from URL if needed
    yt_match = re.search(r'(?:v=|/)([0-9A-Za-z_-]{11})', target)
    video_id = yt_match.group(1) if yt_match else target
    test_url = f"https://www.youtube.com/watch?v={video_id}"

    status_msg = await update.message.reply_text(f"<tg-emoji emoji-id=\"5807468998840814853\">🔍</tg-emoji> Debugging formats for <code>{html.escape(video_id)}</code>...", parse_mode='HTML')

    loop = asyncio.get_running_loop()
    report_parts = []

    # --- Run with cookies ---
    cookies_path = _ensure_cookies_file(user_id)
    has_cookies = bool(cookies_path)

    async def _list_formats(label: str, opts: dict) -> str:
        def _run():
            import io as _io
            import contextlib
            buf = _io.StringIO()
            try:
                list_opts = {**opts, 'listformats': True, 'quiet': False, 'no_warnings': False}
                # Capture yt-dlp output
                with contextlib.redirect_stdout(buf):
                    with yt_dlp.YoutubeDL(list_opts) as ydl:
                        ydl.extract_info(test_url, download=False)
            except SystemExit:
                pass
            except Exception as e:
                buf.write(f"\nERROR: {e}")
            return buf.getvalue()
        output = await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=30)
        # Trim to fit Telegram message limits
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncated)"
        return f"<b>{label}</b>\n<pre>{html.escape(output) or '(no output)'}</pre>"

    try:
        # Show yt-dlp version
        report_parts.append(f"<b>yt-dlp version:</b> <code>{yt_dlp.version.__version__}</code>")

        base = {'noplaylist': True, **base_yt_opts()}

        # Test without cookies first
        report_parts.append(await _list_formats("Without cookies:", {**base}))

        if has_cookies:
            # Test with cookies + default clients (our actual config)
            report_parts.append(await _list_formats(
                "With cookies (default clients):",
                {**base, 'cookiefile': cookies_path},
            ))
        else:
            report_parts.append("<b>No cookies set</b> — skipping authenticated tests.")

        full_report = "\n\n".join(report_parts)
        # Split if too long for a single Telegram message
        if len(full_report) > 4000:
            for part in report_parts:
                await update.message.reply_text(part, parse_mode='HTML')
            await status_msg.delete()
        else:
            await status_msg.edit_text(full_report, parse_mode='HTML')

    except asyncio.TimeoutError:
        await status_msg.edit_text("<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Debug timed out (30s).", parse_mode='HTML')
    except Exception as e:
        logging.error(f"Debug command error: {e}", exc_info=True)
        await status_msg.edit_text(f"<tg-emoji emoji-id=\"5348027250446967673\">❌</tg-emoji> Debug error: <pre>{html.escape(str(e)[:500])}</pre>", parse_mode='HTML')


async def _post_init(application):
    # Worker threads for downloads, searches and metadata lookups.
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="worker")
    )


async def _on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Unhandled error while processing an update", exc_info=context.error)


def _clear_download_dir():
    """Remove leftovers of downloads that were interrupted by a restart."""
    try:
        for fname in os.listdir(DOWNLOAD_DIR):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, fname))
            except OSError:
                pass
    except OSError:
        pass


if __name__ == '__main__':
    if not TOKEN:
        print("Error: TOKEN not found in .env file")
        exit(1)

    _clear_download_dir()

    # concurrent_updates: one slow download no longer blocks every other user and button.
    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .build()
    )
    application.add_error_handler(_on_error)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("lucky", lucky_command))
    application.add_handler(CommandHandler("setcookies", setcookies_command))
    application.add_handler(CommandHandler("debug", debug_command))
    application.add_handler(CommandHandler("now", now_command))
    application.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result_handler))

    print("Music Bot Started...")
    print("Inline mode enabled! Use @ytmusdbot in any chat to search for music.")
    application.run_polling()
