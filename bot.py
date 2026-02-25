import os
import re
import logging
import asyncio
import tempfile
import urllib.request
import urllib.parse
import json
import hashlib
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
DUMP_CHAT_ID = os.getenv("DUMP_CHAT_ID")  # Chat ID for caching audio files

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "musicbot_temp")

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

# Inline query cache — plain dict so old inline results always remain clickable
INLINE_SEARCH_CACHE: dict = {}

USER_SETTINGS_FILE = "users_data.json"
AWAITING_STATS_FM_USERNAME = set()

_settings_cache: dict | None = None  # in-memory cache — avoids disk reads on every setting access

# --- DATABASE FOR AUDIO FILE CACHING ---
DB_PATH = "audio_cache.db"
_cached_audio_db = None
_cached_audio_model = None

def init_audio_cache():
    """Initialize the audio cache database."""
    global _cached_audio_db, _cached_audio_model
    try:
        db = SqliteDatabase(DB_PATH)

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
    """Get cached audio file ID from database (non-blocking)."""
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
    """Upsert audio file ID to cache database (non-blocking)."""
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

def load_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        if os.path.exists(USER_SETTINGS_FILE):
            with open(USER_SETTINGS_FILE, "r") as f:
                _settings_cache = json.load(f)
        else:
            _settings_cache = {}
    return _settings_cache

def save_settings(data: dict):
    global _settings_cache
    _settings_cache = data
    with open(USER_SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def get_user_setting(user_id, key, default=None):
    data = load_settings()
    user_str = str(user_id)
    return data.get(user_str, {}).get(key, default)

def set_user_setting(user_id, key, value):
    data = load_settings()
    data.setdefault(str(user_id), {})[key] = value
    save_settings(data)

# --- PLATFORM DETECTION ---
YTDLP_DIRECT_PATTERNS = [
    r'youtube\.com/watch',
    r'youtu\.be/',
    r'music\.youtube\.com',
    r'soundcloud\.com',
    r'vk\.com/audio',
    r'vk\.com/music',
]

SONGLINK_RESOLVE_PATTERNS = [
    r'open\.spotify\.com',
    r'spotify\.link',
    r'music\.yandex\.',
    r'music\.apple\.com',
    r'tidal\.com',
    r'deezer\.com',
    r'song\.link',
    r'odesli\.co',
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

# Initialize audio cache
init_audio_cache()


# --- HELPERS: URL DETECTION ---
def is_url(text: str) -> bool:
    t = text.strip()
    return t.startswith("http://") or t.startswith("https://")


def get_url_type(url: str) -> str:
    for pattern in YTDLP_DIRECT_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return 'direct'
    for pattern in SONGLINK_RESOLVE_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return 'resolve'
    return 'direct'  # Unknown URLs — attempt direct download


# --- HELPER: RESOLVE VIA SONG.LINK (ODESLI) API ---
def resolve_via_songlink(url: str) -> dict:
    api_url = f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(url)}&userCountry=US"
    req = urllib.request.Request(api_url, headers={"User-Agent": "MusicBot/1.0"})
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode())

    links_by_platform = data.get("linksByPlatform", {})
    youtube_url = None

    for platform in ("youtube", "youtubeMusic"):
        platform_data = links_by_platform.get(platform)
        if platform_data:
            youtube_url = platform_data.get("url")
            break

    if not youtube_url:
        raise ValueError("No YouTube equivalent found via song.link for this URL.")

    entity = next((e for e in data.get("entitiesByUniqueId", {}).values() if e.get("title")), {})
    return {
        "youtube_url": youtube_url,
        "title": entity.get("title", "Unknown Track"),
        "artist": entity.get("artistName", "Unknown Artist"),
    }


# --- HELPER: GET FULL SONG.LINK URL ---
def get_songlink_url(
    video_id: str,
    original_url: str = None,
    title_hint: str = None,
    artist: str = None,
    track: str = None,
) -> str | None:
    fallback = f"https://song.link/y/{video_id}"
    try:
        url = original_url
        # For non-Spotify/Apple Music URLs, use iTunes to get a canonical streaming URL
        if not url or 'youtube.com' in url or 'youtu.be' in url:
            # Build the cleanest possible search query
            if artist and track:
                clean_track = re.sub(r'[\(\[].*?[\)\]]|official|video|audio|lyrics', '', track, flags=re.IGNORECASE).strip()
                itunes_term = f"{artist} {clean_track}"
            elif title_hint:
                itunes_term = re.sub(r'[\(\[].*?[\)\]]|official|video|audio|lyrics', '', title_hint, flags=re.IGNORECASE).strip()
            else:
                itunes_term = None

            if itunes_term:
                try:
                    itunes_data = _http_get_json(
                        f"https://itunes.apple.com/search?term={urllib.parse.quote(itunes_term)}&limit=5&entity=song",
                        timeout=5
                    )
                    if itunes_data and itunes_data.get("resultCount", 0) > 0:
                        results = itunes_data["results"]
                        # Filter by artist name when we have it (avoids wrong-artist matches)
                        if artist:
                            artist_lower = artist.lower()
                            artist_matches = [
                                r for r in results
                                if artist_lower in r.get("artistName", "").lower()
                                or r.get("artistName", "").lower() in artist_lower
                            ]
                            if artist_matches:
                                results = artist_matches
                        # Among remaining candidates, prefer those with an ISRC
                        with_isrc = [r for r in results if r.get("isrc")]
                        best = with_isrc[0] if with_isrc else results[0]
                        url = best.get("trackViewUrl")
                except Exception as e:
                    logging.warning(f"iTunes fallback search failed: {e}")

        if not url:
            url = f"https://music.youtube.com/watch?v={video_id}"
        data = _http_get_json(
            f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(url)}",
            timeout=8
        )
        return data.get("pageUrl", fallback) if data else fallback
    except Exception as e:
        logging.error(f"Failed to fetch song.link URL: {e}")
        return fallback




# --- HELPER: GET YOUTUBE VIDEO ID FROM SPOTIFY URL VIA SONG.LINK ---
def get_youtube_id_from_spotify_url(spotify_url: str) -> str | None:
    """
    Use song.link API to get the YouTube/YouTube Music video ID from a Spotify URL.
    This provides more accurate matching than search.
    """
    try:
        api_url = f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(spotify_url)}&userCountry=US"
        data = _http_get_json(api_url, timeout=10)

        if not data:
            return None

        # Try to get YouTube Music link first (more accurate for music)
        links_by_platform = data.get("linksByPlatform", {})

        # Prefer YouTube Music for better audio quality/matching
        for platform in ("youtubeMusic", "youtube"):
            platform_data = links_by_platform.get(platform)
            if platform_data:
                url = platform_data.get("url", "")
                # Extract video ID from URL
                match = re.search(r'[?&]v=([^&]+)', url)
                if match:
                    return match.group(1)
                # Handle youtu.be URLs
                match = re.search(r'youtu\.be/([^?&]+)', url)
                if match:
                    return match.group(1)

        return None
    except Exception as e:
        logging.error(f"Failed to get YouTube ID from Spotify URL: {e}")
        return None


# --- HELPER: SEARCH YTMUSIC WITH CLEANED TITLE ---
def _search_ytmusic_for_track(artist: str, title: str) -> dict | None:
    """
    Search YTMusic for a track, stripping noise from the title (brackets, keywords).
    Returns the first matching result or None.
    """
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
                    'source': '🎵'
                }
    except Exception as e:
        logging.error(f"YTMusic track search failed: {e}")
    return None


# --- HELPER: ACCURATE SPOTIFY TO YOUTUBE SEARCH ---
def search_spotify_track_on_youtube(artist: str, title: str, spotify_url: str = None) -> dict | None:
    """
    Search for a Spotify track on YouTube with maximum accuracy.
    Uses multiple strategies: song.link direct lookup, ISRC search, and fuzzy search.
    """
    # Strategy 1: If we have a Spotify URL, use song.link to get direct YouTube mapping
    if spotify_url:
        try:
            youtube_id = get_youtube_id_from_spotify_url(spotify_url)
            if youtube_id:
                # Verify the video exists by getting info
                opts = {'quiet': True, 'noplaylist': True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"https://music.youtube.com/watch?v={youtube_id}", download=False)
                    title_i, artist_i = yt_metadata(info)
                    return {
                        'id': youtube_id,
                        'title': title_i,
                        'uploader': artist_i,
                            'duration_string': format_duration(info.get('duration')),
                            'duration': info.get('duration', 0),
                            'source': '🎯'  # Direct match indicator
                        }
        except Exception as e:
            logging.warning(f"Direct song.link lookup failed: {e}")

    # Strategy 2: Clean-title YTMusic search (strips "Official Video", brackets, etc.)
    try:
        result = _search_ytmusic_for_track(artist, title)
        if result:
            result['source'] = '🎯'
            return result
    except Exception as e:
        logging.warning(f"Clean-title search failed: {e}")

    # Strategy 3: Enhanced search with artist and title
    try:
        # Search YouTube Music specifically for better audio quality
        search_query = f"{artist} {title}"
        ytmusic_results = ytmusic.search(search_query, filter="songs", limit=5)

        best_match = None
        best_score = 0

        for item in ytmusic_results:
            yt_title = item.get('title', '').lower()
            yt_artist = item.get('artists', [{}])[0].get('name', '').lower() if item.get('artists') else ''

            # Calculate match score
            score = 0
            title_lower = title.lower()
            artist_lower = artist.lower()

            # Title match
            if title_lower in yt_title or yt_title in title_lower:
                score += 3
            # Artist match
            if artist_lower in yt_artist or yt_artist in artist_lower:
                score += 2
            # Word overlap
            title_words = set(title_lower.split())
            yt_title_words = set(yt_title.split())
            score += len(title_words & yt_title_words)

            if score > best_score:
                best_score = score
                best_match = item

        if best_match:
            return {
                'id': best_match['videoId'],
                'title': best_match.get('title', title),
                'uploader': best_match.get('artists', [{}])[0].get('name', artist) if best_match.get('artists') else artist,
                'duration_string': best_match.get('duration', '??:??'),
                'duration': best_match.get('duration_seconds', 0),
                'source': '🎵' if best_score >= 5 else '📺'
            }

    except Exception as e:
        logging.error(f"Enhanced search failed: {e}")

    return None


# --- HELPER: COMMON YT-DLP OPTS ---
def get_ydl_opts() -> dict:
    return {
        'format': 'bestaudio/best',
        'outtmpl': f'{DOWNLOAD_DIR}/%(id)s.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
        'noplaylist': True,
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
        'http_chunk_size': 10485760,
    }


# --- HELPER: DOWNLOAD DIRECTLY FROM A URL ---
def download_from_url(url: str) -> dict:
    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=True)


def get_url_info(url: str) -> dict:
    """Extract metadata only (no download) — used to get video_id for cache lookup."""
    opts = {'quiet': True, 'no_warnings': True, 'noplaylist': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# --- HELPER: FORMAT DURATION ---
def format_duration(seconds):
    if seconds is None:
        return "??:??"
    try:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"
    except (ValueError, TypeError):
        return "??:??"


def clean_uploader(name: str) -> str:
    """Strip YouTube Music auto-generated topic-channel suffix: 'Artist - Topic' → 'Artist'."""
    return re.sub(r'\s*-\s*Topic\s*$', '', name, flags=re.IGNORECASE).strip()


def _clean_video_title(title: str, artist: str = None) -> str:
    """
    Strip YouTube video title noise when falling back from yt-dlp's raw 'title' field.
    Handles patterns like:
      "Charli xcx - Apple (Official Video)"  → "Apple"
      "Track Name [Official Music Video] HD"  → "Track Name"
      "Track Name | Artist - Topic"           → "Track Name"
    """
    cleaned = title

    # Strip bracketed/parenthesized tags: (Official Video), [Lyric Video], [HD], etc.
    cleaned = re.sub(
        r'\s*[\(\[][^\)\]]*?\b(official|lyrics?|video|audio|music\s+video|mv|hd|4k|explicit)\b[^\)\]]*[\)\]]',
        '', cleaned, flags=re.IGNORECASE
    ).strip()

    # Strip "| anything" suffix (e.g. "Title | ArtistName - Topic")
    cleaned = re.sub(r'\s*\|.*$', '', cleaned).strip()

    # If starts with "Artist - Title", strip the artist prefix
    if artist:
        artist_strip = re.escape(artist.strip())
        cleaned = re.sub(rf'^\s*{artist_strip}\s*[-–—]\s*', '', cleaned, flags=re.IGNORECASE).strip()

    return cleaned or title  # never return empty — fall back to original


def yt_metadata(info: dict) -> tuple[str, str]:
    """
    Extract clean title and artist from a yt-dlp info dict.
    Prefers the structured 'track'/'artist' fields (populated from YouTube Music metadata)
    over the raw 'title'/'uploader' fields (video title and channel name).
    When falling back to 'title', noise is stripped via _clean_video_title().
    """
    artist = info.get('artist') or clean_uploader(info.get('uploader') or 'Unknown Artist')
    if info.get('track'):
        title = info['track']
    else:
        title = _clean_video_title(info.get('title') or 'Unknown Track', artist=artist)
    return title, artist


# --- HELPER: UNIFIED DOWNLOAD AND UPLOAD FLOW ---
async def process_download_and_upload(
    update_or_query, context, user_id: int, video_id: str,
    status_msg,  # the message to edit with progress ("Downloading...", "Uploading...")
    fallback_title: str = None, fallback_artist: str = None,
    original_url: str = None,
    delete_status_on_success: bool = True
) -> bool:
    """
    Handles the standard flow:
    1. Start concurrent song.link fetch (if enabled)
    2. Download audio via yt-dlp (with timeout)
    3. Extract clean metadata
    4. Upload via send_downloaded_audio
    5. Clean up status message
    """
    # Start song.link fetch in background — will run concurrently with yt-dlp download + ffmpeg
    songlink_future = None
    if get_user_setting(user_id, "include_song_link", False):
        songlink_future = asyncio.get_running_loop().run_in_executor(
            None, lambda: get_songlink_url(video_id, original_url=original_url, artist=fallback_artist, track=fallback_title)
        )

    await status_msg.edit_text(text="⬇️ Downloading...", parse_mode='HTML')

    try:
        loop = asyncio.get_running_loop()
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: download_song(get_ydl_opts(), video_id)),
                timeout=300
            )
        except asyncio.TimeoutError:
            logging.error(f"Download timed out for video_id: {video_id}")
            await status_msg.edit_text("❌ Download timed out. The file may be too large or the connection is slow.")
            return False

        # Prefer yt-dlp's structured music fields, fall back to inputs
        info_title, info_artist = yt_metadata(info)
        song_title = fallback_title if fallback_title and fallback_title != 'Unknown Track' else info_title
        uploader = fallback_artist if fallback_artist and fallback_artist != 'Unknown Artist' else info_artist
        duration = info.get('duration')

        # Collect song.link result (download was the long wait — should be ready now)
        caption = None
        if songlink_future is not None:
            try:
                sl_url = await asyncio.wait_for(asyncio.shield(songlink_future), timeout=3)
                if sl_url:
                    caption = f"<a href='{sl_url}'>song.link</a>"
            except (asyncio.TimeoutError, Exception):
                pass  # Don't block upload if song.link is slow

        await status_msg.edit_text(text="⬆️ Uploading...", parse_mode='HTML')

        success = await send_downloaded_audio(
            update_or_query, context, user_id, video_id,
            song_title, uploader, duration,
            title_hint=f"{uploader} {song_title}",
            caption=caption
        )

        if success:
            if delete_status_on_success:
                await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Error: File not found after download.")
        return success

    except Exception as e:
        logging.error(f"Download/Upload flow error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")
        return False


# --- HELPER: HYBRID SEARCH ---
def search_hybrid(query):
    combined_results = []
    seen_ids = set()

    try:
        music_results = ytmusic.search(query, filter="songs", limit=3)[:3]
        for item in music_results:
            video_id = item['videoId']
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            combined_results.append({
                'id': video_id,
                'title': item['title'],
                'uploader': item['artists'][0]['name'] if 'artists' in item else "Unknown",
                'duration_string': item.get('duration', '??:??'),
                'duration': item.get('duration_seconds', 0),
                'source': '🎵'
            })
    except Exception as e:
        logging.error(f"YTMusic Search failed: {e}")

    # Only fetch as many YT results as needed to reach 10 total
    needed = max(0, 10 - len(combined_results))
    if needed > 0:
        try:
            opts = {'quiet': True, 'noplaylist': True, 'extract_flat': True}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{needed}:{query}", download=False)
                yt_entries = info.get('entries', [])
                for item in yt_entries:
                    video_id = item['id']
                    if video_id in seen_ids:
                        continue
                    seen_ids.add(video_id)
                    dur = format_duration(item.get('duration'))
                    combined_results.append({
                        'id': video_id,
                        'title': item['title'],
                        'uploader': clean_uploader(item.get('uploader', 'YouTube')),
                        'duration_string': dur,
                        'duration': item.get('duration', 0),
                        'source': '📺'
                    })
        except Exception as e:
            logging.error(f"YouTube Search failed: {e}")

    return combined_results


# --- HELPER: BUILD DISPLAY (Text + Keyboard) ---
def build_display(results, page, total_pages):
    items = results[page * 5:(page + 1) * 5]

    def fmt(idx, item):
        src, title, uploader, dur = item['source'], item['title'], item['uploader'], item['duration_string']
        prefix = f"{idx + 1}." if src == '📺' else f"{idx + 1}. {src}"
        return f"{prefix} <b>{title}</b>\n    └ {uploader} ({dur})"

    message_text = "\n\n".join(fmt(i, it) for i, it in enumerate(items))
    message_text += f"\n\n📖 <i>Page {page+1} of {total_pages}</i>"

    number_row = [InlineKeyboardButton(str(i + 1), callback_data=f"dl:{it['id']}") for i, it in enumerate(items)]
    nav_row = [
        InlineKeyboardButton("⬅️", callback_data=f"page:{page-1}") if page > 0 else InlineKeyboardButton("🚫", callback_data="noop"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
        InlineKeyboardButton("➡️", callback_data=f"page:{page+1}") if page < total_pages - 1 else InlineKeyboardButton("🚫", callback_data="noop"),
    ]
    return message_text, InlineKeyboardMarkup([number_row, nav_row])



# --- HELPER: FIND ACTUAL OUTPUT FILE ---
def find_output_file(directory: str, video_id: str) -> str | None:
    """
    yt-dlp + ffmpeg sometimes produce filenames like `<id>.mp3`, `<id>.webm.mp3`, etc.
    This scans the temp dir for any mp3 containing the video_id in its name.
    """
    for fname in os.listdir(directory):
        if video_id in fname and fname.endswith('.mp3'):
            return os.path.join(directory, fname)
    return None

def cleanup_files(video_id: str | None):
    """Deletes all temporary files matching the video_id."""
    if not video_id:
        return
    for fname in os.listdir(DOWNLOAD_DIR):
        if video_id in fname:
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, fname))
            except Exception:
                pass


# --- HELPER: BLOCKING HTTP GET → JSON ---
def _http_get_json(url: str, timeout: int = 10) -> dict | None:
    """Synchronous HTTP GET returning parsed JSON, or None on 404. Raises on other errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "MusicBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise



# --- HELPER: DOWNLOAD SONG BY VIDEO ID ---
def download_song(opts: dict, video_id: str) -> dict:
    url = f"https://music.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


# Sentinel meaning "no caption pre-built — fetch song.link internally if setting is on"
_UNSET = object()


async def send_downloaded_audio(
    update_or_query, context, user_id: int, video_id: str,
    song_title: str, uploader: str, duration: int,
    original_url: str = None, title_hint: str = None,
    caption=_UNSET,  # pass a string to skip internal song.link fetch; pass None to suppress caption
) -> bool:
    """Helper to send audio — uses DB cache if available, otherwise uploads from disk."""

    # Build caption with song.link if not pre-supplied
    if caption is _UNSET:
        caption = None
        if video_id and get_user_setting(user_id, "include_song_link", False):
            loop = asyncio.get_running_loop()
            songlink_url = await loop.run_in_executor(
                None, lambda: get_songlink_url(
                    video_id, original_url, title_hint,
                    artist=uploader, track=song_title
                )
            )
            caption = f"<a href='{songlink_url}'>song.link</a>"

    # Resolve chat_id
    if hasattr(update_or_query, 'effective_chat') and update_or_query.effective_chat:
        chat_id = update_or_query.effective_chat.id
    else:
        chat_id = update_or_query.message.chat_id

    # --- Check DB cache first — avoids file I/O for tracks already uploaded ---
    cached = await get_cached_audio(video_id)
    if cached and cached.tg_file_id:
        logging.info(f"Sending from cache for video_id: {video_id}")
        # Thumbnail is already stored in Telegram with the cached file; no need to re-attach
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=cached.tg_file_id,
            title=song_title,
            performer=uploader,
            duration=duration,
            caption=caption,
            parse_mode='HTML',
            read_timeout=120,
            write_timeout=120
        )
        return True

    # Not cached — need the downloaded file on disk
    actual_file = find_output_file(DOWNLOAD_DIR, video_id)
    if not actual_file:
        return False

    # Upload from file
    with open(actual_file, 'rb') as audio_file:
        sent_msg = await context.bot.send_audio(
            chat_id=chat_id,
            audio=audio_file,
            title=song_title,
            performer=uploader,
            duration=duration,
            caption=caption,
            parse_mode='HTML',
            read_timeout=120,
            write_timeout=120
        )
        tg_file_id = sent_msg.audio.file_id

        # Cache to dump chat if configured
        if DUMP_CHAT_ID and tg_file_id:
            try:
                await context.bot.send_audio(
                    chat_id=DUMP_CHAT_ID,
                    audio=tg_file_id,
                    title=song_title,
                    performer=uploader,
                    duration=duration,
                )
                await save_cached_audio(video_id, tg_file_id, song_title, uploader, duration)
                logging.info(f"Cached audio for video_id: {video_id}")
            except Exception as e:
                logging.warning(f"Failed to cache audio to dump chat: {e}")

    return True


# --- HELPER: GET CURRENT/LAST PLAYED TRACK FROM STATS.FM ---
async def get_statsfm_track_info(user_id: int) -> dict | None:
    """
    Get currently playing or last played track info from stats.fm.
    Returns dict with track info or None if not available.
    """
    username = get_user_setting(user_id, "stats_fm_username")
    if not username or username == "None":
        return None
    
    try:
        loop = asyncio.get_running_loop()
        
        # Try current track first
        current_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/current"
        data = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _http_get_json(current_url)),
            timeout=15
        )
        
        track_info = None
        status_text = None
        
        if data and data.get("item"):
            track_info = data["item"].get("track", {})
            status_text = "(stats.fm)"
        else:
            # Fallback to recent track
            recent_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/recent"
            recent_data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _http_get_json(recent_url)),
                timeout=15
            )
            if recent_data and recent_data.get("items"):
                track_info = recent_data["items"][0].get("track", {})
                status_text = "🕐 Last Played"
        
        if not track_info:
            return None
        
        track_name = track_info.get("name", "")
        artists = track_info.get("artists", [])
        artist_name = artists[0].get("name", "") if artists else ""
        spotify_ids = track_info.get("externalIds", {}).get("spotify", [])
        spotify_url = f"https://open.spotify.com/track/{spotify_ids[0]}" if spotify_ids else None
        
        return {
            "track_name": track_name,
            "artist_name": artist_name,
            "search_query": f"{artist_name} {track_name}".strip(),
            "spotify_url": spotify_url,
            "status_text": status_text,
            "has_statsfm": True
        }
        
    except Exception as e:
        logging.error(f"Error getting stats.fm track info: {e}")
        return None


# Bump this string to immediately invalidate all Telegram-cached inline result IDs.
# Old cached entries under different IDs will be ignored; new queries generate fresh results.
INLINE_RESULT_VERSION = "v1"

# --- INLINE MODE HELPERS ---
def get_inline_result_id(video_id: str) -> str:
    """Generate a unique result ID for inline queries."""
    return hashlib.md5((video_id + INLINE_RESULT_VERSION).encode()).hexdigest()[:16]

def get_loading_markup(video_id: str) -> InlineKeyboardMarkup:
    """Create a loading markup for inline results - required for editing later."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text="⏳ Wait...", callback_data=f"loading:{video_id}")]
    ])


def create_inline_audio_result(track: dict, result_id: str = None) -> InlineQueryResultAudio:
    """Create an InlineQueryResultAudio from track data."""
    if result_id is None:
        result_id = get_inline_result_id(track['id'])

    INLINE_SEARCH_CACHE[result_id] = track.copy()

    # Use a 1-second silent MP3 as placeholder - will be replaced when selected
    placeholder_url = "https://github.com/MerrcuL/music-telegram-bot/raw/refs/heads/main/placeholder.mp3"
    return InlineQueryResultAudio(
        id=result_id,
        audio_url=placeholder_url,
        title=track['title'],
        performer=track['uploader'],
        audio_duration=track.get('duration', 0),
        reply_markup=get_loading_markup(track['id']),  # Required for editing later
    )


# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        "👋 Welcome to <b>Simple Music Bot</b>.\n\n"
        "🔎 Send a song name to search.\n"
        "🔗 Send a link to download directly.\n"
        "💬 Use @botusername in any chat to search inline.\n\n"
        "Use /help for full instructions.",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        "📖 <b>How to use this bot</b>\n\n"
        "<b>⚙️ Settings & integrations</b>\n"
        "  • /settings — Configure stats.fm and song.link options\n"
        "  • /now — Download your current or last played Spotify track (requires stats.fm binding)\n\n"
        "<b>🔎 Search</b>\n"
        "Send any song name and the bot will search YouTube Music and YouTube.\n"
        "Example: <i>Numb Linkin Park</i>\n"
        "  • Results are shown in pages of 5\n"
        "  • 🎵 = YouTube Music result\n"
        "  • The rest are all YouTube results\n"
        "  • Tap a number to download that track\n\n"
        "<b>🔗 Download by link</b>\n"
        "Send a URL and the bot downloads it directly.\n"
        "Supported platforms:\n"
        "  • YouTube / YouTube Music — direct\n"
        "  • SoundCloud — direct\n"
        "  • VK Music — direct\n"
        "  • Spotify — resolved via song.link\n"
        "  • Yandex Music — resolved via song.link\n"
        "  • Apple Music, Tidal, Deezer — resolved via song.link\n"
        "  • song.link / odesli.co links\n\n"
        "<b>💬 Inline Mode</b>\n"
        "Type @botusername in any chat to search for music.\n"
        "  • Empty query shows your currently playing / last played track (if stats.fm connected)\n"
        "  • Type song name to search\n\n"
        "<b>🔗 Caption Options</b>\n"
        "In /settings you can enable:\n"
        "  • song.link — Adds universal song.link URL to captions",
        parse_mode='HTML'
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point — routes to link handler or search handler."""
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    text = update.message.text.strip()
    if not text:
        return

    if user_id in AWAITING_STATS_FM_USERNAME:
        AWAITING_STATS_FM_USERNAME.discard(user_id)
        if not text or text.startswith("/"):
            await update.message.reply_text("❌ Invalid username. Use /settings to try again.")
            return
        set_user_setting(user_id, "stats_fm_username", text)
        await update.message.reply_text(f"✅ stats.fm username saved as <b>{text}</b>.", parse_mode='HTML')
        return

    if is_url(text):
        await handle_link(update, context, text)
    else:
        await handle_search(update, context, text)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Handles direct URL download — routes through song.link if needed."""
    user_id = update.effective_user.id
    loop = asyncio.get_running_loop()
    video_id = None
    status_msg = await update.message.reply_text("🔗 Processing link...")

    try:
        url_type = get_url_type(url)
        download_url = url
        resolved_title = None
        resolved_artist = None

        if url_type == 'resolve':
            await status_msg.edit_text("🔍 Resolving link via song.link...")
            try:
                resolved = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: resolve_via_songlink(url)),
                    timeout=15
                )
                download_url = resolved["youtube_url"]
                resolved_title = resolved.get("title")
                resolved_artist = resolved.get("artist")
                await status_msg.edit_text(
                    f"✅ Resolved: <b>{resolved_title}</b> — {resolved_artist}\n💾 Checking cache...",
                    parse_mode='HTML'
                )
            except asyncio.TimeoutError:
                await status_msg.edit_text("❌ song.link API timed out. Try again later.")
                return
            except Exception as e:
                logging.error(f"song.link resolution failed: {e}")
                await status_msg.edit_text(
                    "❌ Could not resolve this link via song.link.\n"
                    "The platform may not be supported or the track is unavailable."
                )
                return

        # --- FAST-PATH ID EXTRACTION ---
        # If it's a YouTube URL (either original or resolved), extract ID via regex 
        # to avoid the 2-3s penalty of yt-dlp network extraction just to check the cache.
        yt_match = re.search(r'(?:v=|/)([0-9A-Za-z_-]{11}).*', download_url)
        if yt_match and ('youtube.com' in download_url or 'youtu.be' in download_url):
            video_id = yt_match.group(1)
            info_only = None
        else:
            try:
                info_only = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: get_url_info(download_url)),
                    timeout=30
                )
                video_id = info_only.get('id', '')
            except Exception as e:
                logging.warning(f"Info-only extraction failed (will still try download): {e}")
                info_only = None

        # --- Cache hit: skip download entirely ---
        if video_id:
            cached_audio = await get_cached_audio(video_id)
            if cached_audio and cached_audio.tg_file_id:
                # If we bypassed get_url_info, we might not have a title/uploader yet,
                # but cache should have it.
                song_title = resolved_title or (info_only.get('title', 'Unknown Track') if info_only else cached_audio.title)
                uploader = resolved_artist or clean_uploader(info_only.get('uploader', 'Unknown Artist') if info_only else cached_audio.performer)
                duration = info_only.get('duration') if info_only else cached_audio.duration
                
                success = await send_downloaded_audio(
                    update, context, user_id, video_id,
                    song_title, uploader, duration,
                    original_url=url, title_hint=f"{uploader} {song_title}"
                )
                if success:
                    await status_msg.delete()
                else:
                    await status_msg.edit_text("❌ Error: could not send cached audio.")
                return

        # --- Cache miss: download and upload ---
        # yt_metadata prefers clean names, but we pass resolved as fallback just in case
        await process_download_and_upload(
            update, context, user_id, video_id,
            status_msg=status_msg,
            fallback_title=resolved_title, fallback_artist=resolved_artist,
            original_url=download_url
        )

    except Exception as e:
        logging.error(f"Link Download Error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")

    finally:
        cleanup_files(video_id)


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Handles text search queries."""
    user_id = update.effective_user.id
    status_msg = await update.message.reply_text(f"🔎 Searching '{query}'...")

    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: search_hybrid(query))

        if not results:
            await status_msg.edit_text("No results found.")
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
    await query.answer()

    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    data = query.data

    if data.startswith("settings:"):
        action = data.split(":")[1]

        if action == "bind_stats_fm":
            AWAITING_STATS_FM_USERNAME.add(user_id)
            await query.answer("💬 Please send your stats.fm username")
            await query.message.reply_text("💬 Please send your stats.fm username (make sure your profile is public):")
            return

        elif action == "toggle_song_link":
            new_val = not get_user_setting(user_id, "include_song_link", False)
            set_user_setting(user_id, "include_song_link", new_val)
            await query.answer(f"song.link {'enabled ✅' if new_val else 'disabled ❌'}")
            await query.edit_message_text(
                f"⚙️ <b>Settings</b>\n\nstats.fm username: <code>{get_user_setting(user_id, 'stats_fm_username', 'None')}</code>\n",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Bind stats.fm account", callback_data="settings:bind_stats_fm")],
                    [InlineKeyboardButton(f"Include song.link: {'ON' if new_val else 'OFF'}", callback_data="settings:toggle_song_link")],
                ]),
                parse_mode='HTML'
            )
            return

    if data.startswith("page:"):
        new_page = int(data.split(":")[1])
        results = SEARCH_CACHE.get(user_id)
        if not results:
            await query.edit_message_text("❌ Search expired. Please search again.")
            return

        total_pages = (len(results) + 4) // 5
        text, reply_markup = build_display(results, new_page, total_pages)
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
        return

    if data == "cancel":
        await query.message.delete()
        if user_id in SEARCH_CACHE:
            del SEARCH_CACHE[user_id]
        return

    if data == "noop":
        return

    if data.startswith("dl:"):
        video_id = data.split(":")[1]

        # Resolve clean metadata from search cache regardless of code path
        results_cache = SEARCH_CACHE.get(user_id, [])
        cached_track = next((r for r in results_cache if r['id'] == video_id), None)

        # --- Cache hit: skip download entirely ---
        cached_audio = await get_cached_audio(video_id)
        if cached_audio and cached_audio.tg_file_id:
            song_title = cached_track['title'] if cached_track else (cached_audio.title or 'Unknown Track')
            uploader = cached_track['uploader'] if cached_track else (cached_audio.performer or 'Unknown Artist')
            duration = cached_audio.duration or (cached_track.get('duration') if cached_track else None)
            try:
                success = await send_downloaded_audio(
                    update, context, user_id, video_id,
                    song_title, uploader, duration,
                    title_hint=f"{uploader} {song_title}"
                )
                if success:
                    await query.message.delete()
                else:
                    await query.message.edit_text("❌ Error: could not send cached audio.")
            except Exception as e:
                logging.error(f"Cache send error: {e}")
                await query.message.edit_text(f"❌ Error: {e}")
            return

        # --- Cache miss: download and upload ---
        await update.callback_query.answer() # Ack the callback so button stops spinning
        _fallback_title = cached_track['title'] if cached_track and cached_track['source'] == '🎵' else None
        _fallback_artist = cached_track['uploader'] if cached_track and cached_track['source'] == '🎵' else None
        
        try:
            await process_download_and_upload(
                update, context, user_id, video_id,
                status_msg=query.message,
                fallback_title=_fallback_title, fallback_artist=_fallback_artist
            )
        except Exception as e:
            logging.error(f"Download Error: {e}")
            await query.message.edit_text(f"❌ Error: {e}")
        finally:
            cleanup_files(video_id)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return
    username = get_user_setting(user_id, "stats_fm_username", "None")
    songlink_on = get_user_setting(user_id, "include_song_link", False)
    await update.message.reply_text(
        f"⚙️ <b>Settings</b>\n\n"
        f"stats.fm username: <code>{username}</code>\n",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Bind stats.fm account", callback_data="settings:bind_stats_fm")],
            [InlineKeyboardButton(f"Include song.link: {'ON' if songlink_on else 'OFF'}", callback_data="settings:toggle_song_link")],
        ]),
        parse_mode='HTML'
    )


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        return

    username = get_user_setting(user_id, "stats_fm_username")
    if not username or username == "None":
        await update.message.reply_text("❌ You haven't bound your stats.fm account yet. Use /settings to bind it.")
        return

    status_msg = await update.message.reply_text(f"🎧 Fetching current track...")

    video_id = None  # ensure cleanup_files always has a value to work with
    try:
        loop = asyncio.get_running_loop()

        # --- Fetch current track ---
        current_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/current"
        data = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _http_get_json(current_url)),
            timeout=15
        )

        track_info = None
        if data and data.get("item"):
            track_info = data["item"].get("track", {})
            status_text = "Currently playing"
        else:
            # Fallback to most recent track
            await status_msg.edit_text(f"🎧 Nothing playing right now. Fetching most recent track...")
            recent_url = f"https://api.stats.fm/api/v1/users/{urllib.parse.quote(username)}/streams/recent"
            recent_data = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: _http_get_json(recent_url)),
                timeout=15
            )
            if recent_data and recent_data.get("items"):
                track_info = recent_data["items"][0].get("track", {})
                status_text = "Last played"

        if not track_info:
            await status_msg.edit_text("❌ No recent tracks found or stats.fm profile is private.")
            return

        track_name = track_info.get("name", "")
        artists = track_info.get("artists", [])
        artist_name = artists[0].get("name", "") if artists else ""
        spotify_ids = track_info.get("externalIds", {}).get("spotify", [])
        spotify_url = f"https://open.spotify.com/track/{spotify_ids[0]}" if spotify_ids else None

        # Search YTMusic directly — stats.fm already gives us clean metadata, no need for song.link here.
        # spotify_url is still passed through so song.link captions prefer the Spotify URL over YouTube.
        search_query = f"{artist_name} {track_name}".strip()
        if not search_query:
            await status_msg.edit_text("❌ Could not extract track information from stats.fm.")
            return

        await status_msg.edit_text(
            f"🔎 {status_text} track:\n<b>{artist_name} - {track_name}</b>",
            parse_mode='HTML'
        )

        # Use accurate search with song.link and ISRC matching
        best_result = await loop.run_in_executor(
            None,
            lambda: search_spotify_track_on_youtube(artist_name, track_name, spotify_url)
        )

        if not best_result:
            # Fallback to hybrid search
            results = await loop.run_in_executor(None, lambda: search_hybrid(search_query))
            if not results:
                await status_msg.edit_text(
                    f"❌ Could not find <b>{artist_name} - {track_name}</b> on YouTube.",
                    parse_mode='HTML'
                )
                return
            best_result = results[0]

        video_id = best_result['id']

        # --- Cache hit: skip download ---
        cached_audio = await get_cached_audio(video_id)
        if cached_audio and cached_audio.tg_file_id:
            song_title = track_name or best_result.get('title', 'Unknown Track')
            uploader = artist_name or best_result.get('uploader', 'Unknown Artist')
            duration = best_result.get('duration')
            success = await send_downloaded_audio(
                update, context, user_id, video_id,
                song_title, uploader, duration,
                original_url=spotify_url, title_hint=f"{artist_name} {track_name}"
            )
            if success:
                await status_msg.delete()
            else:
                await status_msg.edit_text("❌ Error: could not send cached audio.")
            return

        # Start song.link fetch in background — runs concurrently with yt-dlp download
        # spotify_url is the best input for song.link (direct Spotify → cross-platform lookup)
        songlink_future = None
        if get_user_setting(user_id, "include_song_link", False):
            songlink_future = asyncio.get_running_loop().run_in_executor(
                None, lambda: get_songlink_url(
                    video_id, original_url=spotify_url,
                    artist=artist_name, track=track_name
                )
            )

        # --- Cache miss: stream flow directly delegates to download helper ---
        await status_msg.edit_text(f"⬇️ Downloading <b>{best_result['title']}</b>...", parse_mode='HTML')
        await process_download_and_upload(
            update, context, user_id, video_id,
            status_msg=status_msg,
            fallback_title=track_name, fallback_artist=artist_name,
            original_url=spotify_url
        )

    except Exception as e:
        logging.error(f"Now Command Error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")

    finally:
        cleanup_files(video_id)


# --- INLINE MODE HANDLERS ---

async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle inline queries.
    - Empty query: Show currently playing / last played track (if stats.fm connected)
    - Non-empty query: Search for tracks
    """
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.inline_query.answer([], cache_time=0)
        return

    query = update.inline_query.query.strip()
    results = []

    if not query:
        # Empty query - show currently playing / last played track
        statsfm_info = await get_statsfm_track_info(user_id)

        if statsfm_info and statsfm_info.get("has_statsfm"):
            # Use accurate search for the track
            loop = asyncio.get_running_loop()
            try:
                best_result = await loop.run_in_executor(
                    None,
                    lambda: search_spotify_track_on_youtube(
                        statsfm_info["artist_name"],
                        statsfm_info["track_name"],
                        statsfm_info.get("spotify_url")
                    )
                )

                if best_result:
                    # Add status prefix to title
                    track = best_result.copy()
                    track['title'] = f"{statsfm_info['status_text']}: {track['title']}"
                    result = create_inline_audio_result(track)
                    results.append(result)
                else:
                    # Fallback to hybrid search
                    search_results = await loop.run_in_executor(
                        None,
                        lambda: search_hybrid(statsfm_info["search_query"])
                    )
                    if search_results:
                        track = search_results[0].copy()
                        track['title'] = f"{statsfm_info['status_text']}: {track['title']}"
                        result = create_inline_audio_result(track)
                        results.append(result)
                    else:
                        # No results found - show a message result
                        from telegram import InlineQueryResultArticle, InputTextMessageContent
                        results.append(InlineQueryResultArticle(
                            id="no_results",
                            title=f"{statsfm_info['status_text']}: {statsfm_info['artist_name']} - {statsfm_info['track_name']}",
                            description="No results found on YouTube",
                            input_message_content=InputTextMessageContent(
                                f"❌ Could not find <b>{statsfm_info['artist_name']} - {statsfm_info['track_name']}</b> on YouTube.",
                                parse_mode='HTML'
                            )
                        ))
            except Exception as e:
                logging.error(f"Error searching for stats.fm track: {e}")
        else:
            # No stats.fm connected - show help message
            from telegram import InlineQueryResultArticle, InputTextMessageContent
            results.append(InlineQueryResultArticle(
                id="help",
                title="Type a song name to search",
                description="Connect stats.fm in /settings to see your currently playing track here",
                input_message_content=InputTextMessageContent(
                    "💬 <b>Inline Music Search</b>\n\n"
                    "Type @botusername followed by a song name to search.\n"
                    "Connect your stats.fm account in /settings to see your currently playing track here!",
                    parse_mode='HTML'
                )
            ))
    else:
        # Non-empty query - search for tracks
        loop = asyncio.get_running_loop()
        try:
            search_results = await loop.run_in_executor(None, lambda: search_hybrid(query))

            for track in search_results[:20]:  # Limit to 20 results
                result = create_inline_audio_result(track)
                results.append(result)

        except Exception as e:
            logging.error(f"Inline search error: {e}")

    await update.inline_query.answer(results, cache_time=0, is_personal=True)


async def chosen_inline_result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle when a user selects an inline result.
    Download the track and upload it to the dump chat for caching,
    then edit the inline message with the actual audio.
    """
    chosen = update.chosen_inline_result
    user_id = chosen.from_user.id
    result_id = chosen.result_id
    inline_message_id = chosen.inline_message_id

    if user_id not in ALLOWED_USERS:
        logging.warning(f"User {user_id} not in allowed list")
        return

    if not inline_message_id:
        logging.warning("No inline_message_id available, cannot edit message")
        return

    if result_id not in INLINE_SEARCH_CACHE:
        logging.warning(f"Result ID {result_id} not found in cache")
        return

    track = INLINE_SEARCH_CACHE[result_id]
    video_id = track['id']

    logging.info(f"Processing chosen inline result: result_id={result_id}, video_id={video_id}, user_id={user_id}")

    # Check if we have this track cached
    cached = await get_cached_audio(video_id)
    tg_file_id = None

    try:
        if cached and cached.tg_file_id:
            # Use cached file
            logging.info(f"Using cached audio for video_id: {video_id}, file_id: {cached.tg_file_id}")
            tg_file_id = cached.tg_file_id
        else:
            # Need to download and upload to dump chat
            if not DUMP_CHAT_ID:
                logging.error("DUMP_CHAT_ID not configured, cannot cache audio")
                return

            logging.info(f"Downloading and caching audio for video_id: {video_id}")

            loop = asyncio.get_running_loop()

            # Download the track
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: download_song(get_ydl_opts(), video_id)),
                    timeout=300
                )
            except asyncio.TimeoutError:
                logging.error(f"Download timed out for video_id: {video_id}")
                return
            except Exception as e:
                logging.error(f"Download failed for video_id {video_id}: {e}")
                return

            info_title, info_artist = yt_metadata(info)
            song_title = info_title
            uploader = info_artist
            duration = info.get('duration', track.get('duration', 0))

            logging.info(f"Downloaded: {song_title} by {uploader}, duration: {duration}")

            # Find the downloaded file
            actual_file = find_output_file(DOWNLOAD_DIR, video_id)
            if not actual_file:
                logging.error(f"Downloaded file not found for video_id: {video_id}")
                return

            logging.info(f"Found downloaded file: {actual_file}")

            # Upload to dump chat to get file_id
            try:
                with open(actual_file, 'rb') as audio_file:
                    sent_msg = await context.bot.send_audio(
                        chat_id=DUMP_CHAT_ID,
                        audio=audio_file,
                        title=song_title,
                        performer=uploader,
                        duration=duration,
                        read_timeout=120,
                        write_timeout=120,
                    )
                    tg_file_id = sent_msg.audio.file_id
                    logging.info(f"Uploaded to dump chat, got file_id: {tg_file_id}")

                # Save to cache
                await save_cached_audio(video_id, tg_file_id, song_title, uploader, duration)
                logging.info(f"Saved to cache: video_id={video_id}, file_id={tg_file_id}")

            except Exception as e:
                logging.error(f"Failed to upload to dump chat: {e}")
                # Remove the stuck loading button so the user isn't left hanging
                try:
                    await context.bot.edit_message_reply_markup(
                        inline_message_id=inline_message_id,
                        reply_markup=None
                    )
                except Exception:
                    pass
                return
            finally:
                # Cleanup
                cleanup_files(video_id)

        if not tg_file_id:
            logging.error("No tg_file_id available, cannot edit message")
            return

        # Prepare caption with song.link if enabled
        caption = None
        if get_user_setting(user_id, "include_song_link", False):
            songlink_url = get_songlink_url(video_id, title_hint=f"{track['uploader']} {track['title']}")
            caption = f"<a href='{songlink_url}'>song.link</a>"

        # Edit the inline message with the actual audio
        logging.info(f"Editing inline message {inline_message_id} with file_id {tg_file_id}")

        try:
            await context.bot.edit_message_media(
                media=InputMediaAudio(
                    media=tg_file_id,
                    title=track['title'],
                    performer=track['uploader'],
                    duration=track.get('duration', 0),
                ),
                inline_message_id=inline_message_id,
            )
            logging.info("Successfully edited message media")

            if caption:
                await context.bot.edit_message_caption(
                    inline_message_id=inline_message_id,
                    caption=caption,
                    parse_mode='HTML'
                )
                logging.info("Successfully edited message caption")

        except Exception as e:
            logging.error(f"Failed to edit message media: {e}")
            # Don't raise - the user already has the placeholder, we'll just log the error

    except Exception as e:
        logging.error(f"Error handling chosen inline result: {e}", exc_info=True)


if __name__ == '__main__':
    if not TOKEN:
        print("Error: TOKEN not found in .env file")
        exit(1)

    # Increase default thread pool size to handle many concurrent yt-dlp downloads.
    # By default it's based on CPU count, meaning a 4-core machine might only 
    # allow 4 simultaneous downloads before queuing up others.
    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=32)
    loop.set_default_executor(executor)

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("now", now_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Inline mode handlers
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result_handler))

    print("Music Bot Started...")
    print("Inline mode enabled! Use @botusername in any chat to search for music.")
    application.run_polling()