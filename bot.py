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

    entities = data.get("entitiesByUniqueId", {})
    title = "Unknown Track"
    artist = "Unknown Artist"
    for entity in entities.values():
        if entity.get("title"):
            title = entity.get("title", title)
            artist = entity.get("artistName", artist)
            break

    return {"youtube_url": youtube_url, "title": title, "artist": artist}


# --- HELPER: DOWNLOAD DIRECTLY FROM A URL ---
def download_from_url(url: str) -> dict:
    ydl_opts = {
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
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


# --- HELPER: FORMAT DURATION ---
def format_duration(seconds):
    if seconds is None: return "??:??"
    if isinstance(seconds, str) and ":" in seconds: return seconds
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

    try:
        opts = {'quiet': True, 'noplaylist': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch7:{query}", download=False)
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
    start_index = page * 5
    end_index = start_index + 5
    current_page_items = results[start_index:end_index]

    text_lines = []
    for idx, item in enumerate(current_page_items):
        num = idx + 1
        source = item['source']
        title = item['title']
        uploader = item['uploader']
        duration = item['duration_string']

        if source == '📺':
            line = f"{num}. <b>{title}</b>\n    └ {uploader} ({duration})"
        else:
            line = f"{num}. {source} <b>{title}</b>\n    └ {uploader} ({duration})"
        text_lines.append(line)

    message_text = "\n\n".join(text_lines)
    message_text += f"\n\n📖 <i>Page {page+1} of {total_pages}</i>"

    keyboard = []
    number_row = []
    for idx, item in enumerate(current_page_items):
        number_row.append(InlineKeyboardButton(str(idx + 1), callback_data=f"dl:{item['id']}"))
    keyboard.append(number_row)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"page:{page-1}"))
    else:
        nav_row.append(InlineKeyboardButton("🚫", callback_data="noop"))

    nav_row.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"page:{page+1}"))
    else:
        nav_row.append(InlineKeyboardButton("🚫", callback_data="noop"))
    keyboard.append(nav_row)

    return message_text, InlineKeyboardMarkup(keyboard)



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
        "<b>🔎 Search</b>\n"
        "Send any song name and the bot will search YouTube Music and YouTube.\n"
        "Example: <i>Numb Linkin Park</i>\n"
        "  • Results are shown in pages of 5\n"
        "  • 🎵 = YouTube Music result\n"
        "  • 📺 = YouTube result\n"
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

    if is_url(text):
        await handle_link(update, context, text)
    else:
        await handle_search(update, context, text)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    """Handles direct URL download — routes through song.link if needed."""
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
        file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

        # Upload has no timeout — Telegram handles large files slowly but reliably
        await status_msg.edit_text("⬆️ Uploading...")

        # Find the actual output file — ffmpeg may produce a slightly different name
        actual_file = find_output_file(DOWNLOAD_DIR, video_id)
        if actual_file:
            with open(actual_file, 'rb') as audio_file:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio_file,
                    title=song_title,
                    performer=uploader,
                    duration=duration,
                    read_timeout=120,
                    write_timeout=120
                )
            await status_msg.delete()
        else:
            await status_msg.edit_text("❌ Error: File not found after download.")

    except Exception as e:
        logging.error(f"Link Download Error: {e}")
        await status_msg.edit_text(f"❌ Error: {e}")

    finally:
        # Clean up all temp files matching this video_id
        if video_id:
            for fname in os.listdir(DOWNLOAD_DIR):
                if video_id in fname:
                    try:
                        os.remove(os.path.join(DOWNLOAD_DIR, fname))
                    except Exception:
                        pass


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

    data = query.data
    user_id = update.effective_user.id

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

        file_path = os.path.join(DOWNLOAD_DIR, f"{video_id}.mp3")

        ydl_opts = {
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

        try:
            loop = asyncio.get_running_loop()
            # Timeout covers download only — upload is separate
            try:
                info = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: download_song(ydl_opts, video_id)),
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
            actual_file = find_output_file(DOWNLOAD_DIR, video_id)
            if actual_file:
                with open(actual_file, 'rb') as audio_file:
                    await context.bot.send_audio(
                        chat_id=update.effective_chat.id,
                        audio=audio_file,
                        title=song_title,
                        performer=uploader,
                        duration=duration,
                        read_timeout=120,
                        write_timeout=120
                    )
                await query.message.delete()
            else:
                await query.message.edit_text("❌ Error: File not found after download.")


        except Exception as e:
            logging.error(f"Download Error: {e}")
            await query.message.edit_text(f"❌ Error: {e}")

        finally:
            # Clean up all temp files matching this video_id
            for fname in os.listdir(DOWNLOAD_DIR):
                if video_id in fname:
                    try:
                        os.remove(os.path.join(DOWNLOAD_DIR, fname))
                    except Exception:
                        pass


def download_song(opts, video_id):
    url = f"https://www.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


if __name__ == '__main__':
    if not TOKEN:
        print("Error: TOKEN not found in .env file")
        exit(1)

    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))

    print("Music Bot Started...")
    application.run_polling()