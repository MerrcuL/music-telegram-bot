import os
import logging
import asyncio
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CallbackQueryHandler, CommandHandler, filters
from ytmusicapi import YTMusic
import yt_dlp

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv("TOKEN")
DOWNLOAD_DIR = "/dev/shm/musicbot_temp"

ALLOWED_USERS = set()
try:
    user_list = os.getenv("ALLOWED_USERS", "")
    ALLOWED_USERS = {int(x.strip()) for x in user_list.split(",") if x.strip()}
except ValueError:
    print("Error parsing ALLOWED_USERS. Check your .env file.")

# --- IN-MEMORY CACHE ---
SEARCH_CACHE = {} 

# --- LOGGING ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

ytmusic = YTMusic()
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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
    
    # 1. YouTube Music (Top 2)
    try:
        music_results = ytmusic.search(query, filter="songs", limit=3)
        for item in music_results[:3]: 
            combined_results.append({
                'id': item['videoId'],
                'title': item['title'],
                'uploader': item['artists'][0]['name'] if 'artists' in item else "Unknown",
                'duration_string': item.get('duration', '??:??'),
                'source': '🎵' 
            })
    except Exception as e:
        logging.error(f"YTMusic Search failed: {e}")

    # 2. YouTube (Next 7)
    try:
        opts = {'quiet': True, 'noplaylist': True, 'extract_flat': True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch7:{query}", download=False)
            yt_entries = info.get('entries', [])
            for item in yt_entries:
                dur = format_duration(item.get('duration'))
                combined_results.append({
                    'id': item['id'],
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
    """
    Returns a tuple: (text_message, inline_keyboard)
    """
    start_index = page * 5
    end_index = start_index + 5
    current_page_items = results[start_index:end_index]

    # 1. Build the Text Message (The List)
    text_lines = []
    for idx, item in enumerate(current_page_items):
        num = idx + 1
        source = item['source']
        title = item['title']
        uploader = item['uploader']
        duration = item['duration_string']
        
        # --- CONDITIONAL FORMATTING ---
        if source == '📺':
            # YouTube: Title only (Clean look)
            line = f"{num}. <b>{title}</b>\n    └ {uploader} ({duration})"
        else:
            # Music: Title + Artist (Standard look)
            line = f"{num}. {source} <b>{title}</b>\n    └ {uploader} ({duration})"
            
        text_lines.append(line)
    
    message_text = "\n\n".join(text_lines)
    message_text += f"\n\n📖 <i>Page {page+1} of {total_pages}</i>"

    # 2. Build the Keyboard (The Buttons)
    keyboard = []
    
    # Row of Numbers: [1] [2] [3] [4] [5]
    number_row = []
    for idx, item in enumerate(current_page_items):
        num = idx + 1
        number_row.append(InlineKeyboardButton(str(num), callback_data=f"dl:{item['id']}"))
    keyboard.append(number_row)

    # Navigation Row
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

# --- HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text(
        "Welcome to Simple Music Bot.\n\n"
        "Just send the name of a song (e.g., 'Numb Linkin Park') and bot will search for it on YouTube Music & YouTube and send the MP3 file to you."
    )

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS: return

    query = update.message.text
    if not query: return

    status_msg = await update.message.reply_text(f"🔎 Searching '{query}'...")

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, lambda: search_hybrid(query))

        if not results:
            await status_msg.edit_text("No results found.")
            return

        SEARCH_CACHE[user_id] = results
        total_pages = (len(results) + 4) // 5
        
        # Get formatted text and buttons
        text, reply_markup = build_display(results, 0, total_pages)
        
        # Use parse_mode='HTML' to make titles bold
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
            await query.edit_message_text("❌ Search expired.")
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
        
        await query.edit_message_text(text=f"⬇️ Downloading ID: {video_id}.\nWait a moment...")

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
        }

        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: download_song(ydl_opts, video_id))
            
            song_title = info.get('title', 'Unknown Track')
            uploader = info.get('uploader', 'Unknown Artist')
            duration = info.get('duration')

            await query.edit_message_text(text="⬆️ Uploading...")
            
            if os.path.exists(file_path):
                with open(file_path, 'rb') as audio_file:
                    await context.bot.send_audio(
                        chat_id=update.effective_chat.id,
                        audio=audio_file,
                        title=song_title,
                        performer=uploader,
                        duration=duration
                    )
                await query.message.delete()
            else:
                await query.message.edit_text("❌ Error: File not found.")

        except Exception as e:
            logging.error(f"Download Error: {e}")
            await query.message.edit_text(f"❌ Error: {e}")
        
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

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
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(handle_callback))

    print("List-View Music Bot Started...")
    application.run_polling()