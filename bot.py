import os
import re
import logging
import asyncio
import tempfile
import urllib.request
import urllib.parse
import json
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CallbackQueryHandler, CommandHandler, filters
from ytmusicapi import YTMusic
import yt_dlp

try:
    from cachetools import TTLCache
    CACHE_AVAILABLE = True
except ImportError:
    CACHE_AVAILABLE = False

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv("TOKEN")

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "musicbot_temp")

ALLOWED_USERS = set()
try:
    user_list = os.getenv("ALLOWED_USERS", "")
    ALLOWED_USERS = {int(x.strip()) for x in user_list.split(",") if x.strip()}
except ValueError:
    print("Error parsing ALLOWED_USERS. Check your .env file.")

if not ALLOWED_USERS:
    print("WARNING: ALLOWED_USERS is empty. No one will be able to use the bot!")

CACHE_TTL = 600
if CACHE_AVAILABLE:
    SEARCH_CACHE = TTLCache(maxsize=500, ttl=CACHE_TTL)
else:
    print("cachetools not installed. Using plain dict cache (no expiry). Run: pip install cachetools")
    SEARCH_CACHE = {}

USER_SETTINGS_FILE = "users_data.json"
AWAITING_STATS_FM_USERNAME = set()

_settings_cache: dict | None = None  # in-memory cache — avoids disk reads on every setting access

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
    return 'unknown'


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
def get_songlink_url(video_id: str, original_url: str = None, title_hint: str = None) -> str | None:
    try:
        url = original_url
        # If URL is missing or YouTube-based, try iTunes to get a canonical streaming URL
        if not url or 'youtube.com' in url or 'youtu.be' in url:
            if title_hint:
                try:
                    clean_title = re.sub(r'[\(\[].*?[\)\]]|official|video|audio|lyrics', '', title_hint, flags=re.IGNORECASE).strip()
                    itunes_data = _http_get_json(f"https://itunes.apple.com/search?term={urllib.parse.quote(clean_title)}&limit=1&entity=song", timeout=3)
                    if itunes_data and itunes_data.get("resultCount", 0) > 0:
                        url = itunes_data["results"][0].get("trackViewUrl")
                except Exception as e:
                    logging.warning(f"iTunes fallback search failed: {e}")
        if not url:
            url = f"https://music.youtube.com/watch?v={video_id}"
        data = _http_get_json(f"https://api.song.link/v1-alpha.1/links?url={urllib.parse.quote(url)}", timeout=5)
        return data.get("pageUrl", f"https://song.link/y/{video_id}") if data else f"https://song.link/y/{video_id}"
    except Exception as e:
        logging.error(f"Failed to fetch song.link URL: {e}")
        return f"https://song.link/y/{video_id}"


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


# --- HELPER: FORMAT DURATION ---
def format_duration(seconds):
    if seconds is None: return "??:??"
    try:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        return f"{m}:{s:02d}"
    except (ValueError, TypeError):
        return "??:??"


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
                        'uploader': item.get('uploader', 'YouTube'),
                        'duration_string': dur,
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
    if not video_id: return
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
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


async def send_downloaded_audio(
    update_or_query, context, user_id: int, video_id: str,
    song_title: str, uploader: str, duration: int,
    original_url: str = None, title_hint: str = None
) -> bool:
    """Helper to find the downloaded file, build the caption, and send the audio."""
    actual_file = find_output_file(DOWNLOAD_DIR, video_id)
    if not actual_file:
        return False

    caption = None
    if video_id and get_user_setting(user_id, "include_song_link", False):
        songlink_url = get_songlink_url(video_id, original_url, title_hint)
        caption = f"<a href='{songlink_url}'>song.link</a>"

    # Update objects expose .effective_chat; CallbackQuery exposes .message.chat_id
    if hasattr(update_or_query, 'effective_chat') and update_or_query.effective_chat:
        chat_id = update_or_query.effective_chat.id
    else:
        chat_id = update_or_query.message.chat_id

    with open(actual_file, 'rb') as audio_file:
        await context.bot.send_audio(
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
    return True

# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text(
        "👋 Welcome to <b>Simple Music Bot</b>.\n\n"
        "🔎 Send a song name to search.\n"
        "🔗 Send a link to download directly.\n\n"
        "Use /help for full instructions.",
        parse_mode='HTML'
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text(
        "📖 <b>How to use this bot</b>\n\n"
        "<b>⚙️ Settings & integrations</b>\n"
        "  • /settings — Bind stats.fm or toggle song.link URL generation\n"
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
        "  • song.link / odesli.co links",
        parse_mode='HTML'
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Single entry point — routes to link handler or search handler."""
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS: return

    text = update.message.text.strip()
    if not text: return

    if user_id in AWAITING_STATS_FM_USERNAME:
        set_user_setting(user_id, "stats_fm_username", text)  # text is already stripped above
        AWAITING_STATS_FM_USERNAME.remove(user_id)
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

        if url_type == 'resolve':
            await status_msg.edit_text("🔍 Resolving link via song.link...")
            try:
                resolved = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: resolve_via_songlink(url)),
                    timeout=15
                )
                download_url = resolved["youtube_url"]
                await status_msg.edit_text(
                    f"✅ Resolved: <b>{resolved['title']}</b> — {resolved['artist']}\n⬇️ Downloading...",
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
        else:
            await status_msg.edit_text("⬇️ Downloading...")

        # Timeout covers download only — upload is separate
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: download_from_url(download_url)),
                timeout=300
            )
        except asyncio.TimeoutError:
            await status_msg.edit_text("❌ Download timed out. The file may be too large or the connection is slow.")
            return

        song_title = info.get('title', 'Unknown Track')
        uploader = info.get('uploader', 'Unknown Artist')
        duration = info.get('duration')
        video_id = info.get('id', '')

        # Upload has no timeout — Telegram handles large files slowly but reliably
        await status_msg.edit_text("⬆️ Uploading...")

        # Find the actual output file — ffmpeg may produce a slightly different name
        success = await send_downloaded_audio(
            update, context, user_id, video_id, 
            song_title, uploader, duration, 
            original_url=download_url, title_hint=f"{uploader} {song_title}"
        )
        
        if success:
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Error: File not found after download.")

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
            await query.message.reply_text("💬 Please send your stats.fm username (make sure your profile is public):")
            return
            
        elif action == "toggle_song_link":
            new_val = not get_user_setting(user_id, "include_song_link", False)
            set_user_setting(user_id, "include_song_link", new_val)
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
        if user_id in SEARCH_CACHE: del SEARCH_CACHE[user_id]
        return

    if data == "noop": return

    if data.startswith("dl:"):
        video_id = data.split(":")[1]
        await query.edit_message_text(text="⬇️ Downloading...")

        try:
            loop = asyncio.get_running_loop()
            # Timeout covers download only — upload is separate
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: download_song(get_ydl_opts(), video_id)),
                    timeout=300
                )
            except asyncio.TimeoutError:
                logging.error(f"Download timed out for video_id: {video_id}")
                await query.message.edit_text("❌ Download timed out. The file may be too large or the connection is slow.")
                return

            song_title = info.get('title', 'Unknown Track')
            uploader = info.get('uploader', 'Unknown Artist')
            duration = info.get('duration')

            await query.edit_message_text(text="⬆️ Uploading...")

            # Find the actual output file — ffmpeg may produce a slightly different name
            success = await send_downloaded_audio(
                update, context, user_id, video_id, 
                song_title, uploader, duration, 
                title_hint=f"{uploader} {song_title}"
            )
            
            if success:
                await query.message.delete()
            else:
                await query.message.edit_text("❌ Error: File not found after download.")

        except Exception as e:
            logging.error(f"Download Error: {e}")
            await query.message.edit_text(f"❌ Error: {e}")

        finally:
            cleanup_files(video_id)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS: return
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
    if user_id not in ALLOWED_USERS: return

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
            f"🔎 {status_text} track:\n<b>{artist_name} - {track_name}</b>\nStarting download...",
            parse_mode='HTML'
        )
        results = await loop.run_in_executor(None, lambda: search_hybrid(search_query))
        if not results:
            await status_msg.edit_text(
                f"❌ Could not find <b>{artist_name} - {track_name}</b> on YouTube.",
                parse_mode='HTML'
            )
            return

        best_result = results[0]
        video_id = best_result['id']
        await status_msg.edit_text(f"⬇️ Downloading <b>{best_result['title']}</b>...", parse_mode='HTML')

        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: download_song(get_ydl_opts(), video_id)),
                timeout=300
            )
        except asyncio.TimeoutError:
            await status_msg.edit_text("❌ Download timed out. The file may be too large or the connection is slow.")
            return

        song_title = info.get('title', 'Unknown Track')
        uploader = info.get('uploader', 'Unknown Artist')
        duration = info.get('duration')

        await status_msg.edit_text("⬆️ Uploading...")
        success = await send_downloaded_audio(
            update, context, user_id, video_id,
            song_title, uploader, duration,
            original_url=spotify_url,          # Spotify URL preferred for better song.link caption
            title_hint=f"{artist_name} {track_name}"  # clean stats.fm metadata, not yt-dlp title
        )

        if success:
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Error: File not found after download.")

    except Exception as e:
        logging.error(f"Now Command Error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")

    finally:
        cleanup_files(video_id)


if __name__ == '__main__':
    if not TOKEN:
        print("Error: TOKEN not found in .env file")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("now", now_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    print("Music Bot Started...")
    application.run_polling()