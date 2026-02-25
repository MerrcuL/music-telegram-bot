# 🎵 Music Telegram Bot with Inline Mode

A private Telegram bot that searches for songs on YouTube Music and YouTube, downloads them, and sends MP3 files directly to you. Now with **inline mode** support!

## ✨ Features

- **Direct Download & Search**: Query by song name or provide direct links to Spotify, SoundCloud, YouTube, etc.
- **Inline Mode**: Use `@botusername` in any chat to search and share music without leaving the conversation
- **stats.fm Integration**: Bind your stats.fm account to instantly download your currently playing or last played Spotify track
- **Smart Caching**: Audio files are cached in a dummy chat for faster subsequent shares
- **Platform Agnostic Links**: Optionally append Spotify link and/or universal song.link to downloaded tracks
- **Accurate Spotify Matching**: Uses song.link API and ISRC matching for precise Spotify → YouTube track mapping

## 🆕 New: Inline Mode

Type `@botusername` in any chat to:
- **Empty query**: See your currently playing / last played track (if stats.fm is connected)
- **Type a song name**: Search and share music directly in any chat

> **Note about "sent via @bot" label**: This is a Telegram client-side feature that cannot be disabled via API. It's shown to indicate content comes from a bot for security reasons.

## 📋 Prerequisites

1. **Telegram Bot Token**: Get one from [@BotFather](https://t.me/BotFather)
   - Use `/setinline` command to enable inline mode
   - Use `/setinlinefeedback` to enable inline feedback (required for the bot to know when a result is selected)

2. **Dummy Chat for Caching**:
   - Create a private group or channel
   - Add your bot to it
   - Get the chat ID (you can use [@userinfobot](https://t.me/userinfobot) or [@getidsbot](https://t.me/getidsbot))

3. **Python 3.8+**

## 🚀 Installation

1. Clone the repository:
```bash
git clone https://github.com/MerrcuL/music-telegram-bot.git
cd music-telegram-bot
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create a `.env` file:
```env
TOKEN=your_telegram_bot_token_here
ALLOWED_USERS=123456789,987654321
DUMP_CHAT_ID=-1001234567890
```

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TOKEN` | Your Telegram bot token from @BotFather | ✅ Yes |
| `ALLOWED_USERS` | Comma-separated list of Telegram user IDs allowed to use the bot | ✅ Yes |
| `DUMP_CHAT_ID` | Chat ID for caching audio files (required for inline mode) | ⚠️ Required for inline mode |

## 🤖 Setting Up Inline Mode

1. Go to [@BotFather](https://t.me/BotFather)
2. Send `/setinline` and select your bot
3. Set the placeholder text (e.g., "Search for music...")
4. Send `/setinlinefeedback` and select your bot
5. Choose "Enabled" to receive feedback when users select results

## 📖 Usage

### Regular Commands

- **Search**: Send any song name
- **Download by link**: Send a URL from YouTube, Spotify, Yandex Music, etc.
- `/settings` - Configure stats.fm integration and song.link options
- `/now` - Download your currently playing Spotify track (requires stats.fm)
- `/help` - Show help message

### Inline Mode

In any chat, type:
- `@botusername` - Shows your currently playing / last played track (if stats.fm connected)
- `@botusername song name` - Search for a song

When you select a result, the bot will:
1. Download the track (if not cached)
2. Upload it to the dump chat for caching
3. Send the audio to the current chat

## 🗄️ Audio Caching

The bot uses a SQLite database (`audio_cache.db`) to store Telegram file IDs of downloaded tracks. This means:
- **Faster shares**: Subsequent shares of the same track are instant
- **Less bandwidth**: No need to re-download tracks
- **Better reliability**: Cached files are served directly from Telegram's servers

## 🔒 Security

- Whitelist-based access control via `ALLOWED_USERS`
- Only configured users can use the bot
- Private bot to prevent unauthorized access

## 🐛 Troubleshooting

### Inline mode sends placeholder audio instead of actual song

If you select a song in inline mode but receive a silent/placeholder audio file:

1. **Check DUMP_CHAT_ID**: Make sure it's set correctly in your `.env` file
2. **Bot permissions**: Ensure the bot is a member of the dump chat and can send audio files
3. **Check logs**: Look for error messages in the bot logs
4. **Inline feedback**: Make sure you enabled `/setinlinefeedback` in @BotFather

### The downloaded song doesn't match the Spotify track exactly

The bot now uses multiple strategies for accurate matching:
1. **song.link API**: Direct mapping from Spotify to YouTube
2. **ISRC matching**: Uses iTunes API to find the exact recording
3. **Fuzzy search**: Enhanced search with artist + title scoring

If you still get mismatches, the track may not be available on YouTube or may have different metadata.

### Common issues

- **"DUMP_CHAT_ID not configured"**: Add `DUMP_CHAT_ID=-100xxxxxxxxx` to your `.env` file
- **"No inline_message_id available"**: Make sure inline feedback is enabled in @BotFather
- **Download timeouts**: Large files may take longer to download - the bot has a 5-minute timeout

## 📝 License

This project is for personal use. Please respect copyright laws and terms of service of the platforms you download from.
