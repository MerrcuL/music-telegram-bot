# 🎵 Music Telegram Bot with Inline Mode

A private Telegram bot that searches for songs on YouTube Music and YouTube, downloads them, and sends MP3 files directly to you.

## ✨ Features

- **Direct Download & Search**: Query by song name or provide direct links to Spotify, SoundCloud, YouTube, etc.
- **Inline Mode**: Use `@botusername` in any chat to search and share music without leaving the conversation
- **stats.fm & Last.fm Integration**: Bind your stats.fm or Last.fm account to instantly download your currently playing or last played track
- **Smart Caching**: Audio files are cached in a dummy chat for faster subsequent shares
- **Accurate Spotify Matching**: Direct Spotify metadata extraction and YouTube Music matching
- **Genius Integration**: Accurate track and artist matching, high-resolution album artwork (1000x1000), Apple Music links, and rich ID3 tags (album, year, composer, lyricist)
- **Lyrics Support**: Per-user setting to include lyrics in caption (when <= 1024 chars) or as a separate message, fetched directly from LRCLIB and embedded into ID3 USLT tags
- **Streaming Service Links**: Attach inline link buttons (YouTube, Spotify, Apple Music, Yandex Music) directly to sent track messages, configured via `/settings`
- **"Feeling Lucky" Mode**: Instantly download the top YouTube Music search result in chat without showing a selection list (toggle with `/lucky` or in `/settings`)

## 📋 Prerequisites

1. **Telegram Bot Token**: Get one from [@BotFather](https://t.me/BotFather)
   - Use `/setinline` command to enable inline mode
   - Use `/setinlinefeedback` to enable inline feedback (required for the bot to know when a result is selected)

2. **Dummy Chat for Caching**:
   - Create a private group
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

3. Create a `.env` file (you can copy `.env.example`):
```bash
cp .env.example .env
```
And configure your settings:
```env
TOKEN=your_telegram_bot_token_here
ALLOWED_USERS=123456789,987654321
DUMP_CHAT_ID=-1001234567890
GENIUS_ACCESS_TOKEN=
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
```

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `TOKEN` | Your Telegram bot token from @BotFather | ✅ Yes |
| `ALLOWED_USERS` | Comma-separated list of Telegram user IDs allowed to use the bot | ✅ Yes |
| `DUMP_CHAT_ID` | Chat ID for caching audio files (required for inline mode) | ⚠️ Required for inline mode |
| `GENIUS_ACCESS_TOKEN` | Optional Genius API token for high-res cover art, Apple Music matching, and rich metadata | ❌ Optional |
| `SPOTIFY_CLIENT_ID` | Spotify Client ID, needed for the Spotify link button | ❌ Optional |
| `SPOTIFY_CLIENT_SECRET` | Spotify Client Secret, needed for the Spotify link button | ❌ Optional |
| `LASTFM_API_KEY` | Last.fm API Key, needed for the Last.fm integration | ❌ Optional |
| `AUDIO_FORMAT` | `m4a` (default: YouTube's AAC audio without re-encoding, fastest and smallest), `mp3` (re-encoded at 192 kbps) or `opus` (no re-encoding, but Telegram treats it as a voice message) | ❌ Optional |
| `BGUTIL_URL` | URL of the [bgutil PO token server](https://github.com/Brainicism/bgutil-ytdlp-pot-provider), default `http://127.0.0.1:4416` | ❌ Optional |
| `ANON_RETRY_MINUTES` | After a YouTube bot check on an anonymous download, use only cookies for this many minutes (default 60) | ❌ Optional |

### Avoiding YouTube bot checks

YouTube sometimes answers "Sign in to confirm you're not a bot", mostly to server IPs. Two things help:

1. **PO tokens**: run the bgutil server next to the bot and point `BGUTIL_URL` at it:
   ```bash
   docker run --name bgutil-provider -d --restart unless-stopped -p 4416:4416 brainicism/bgutil-ytdlp-pot-provider
   ```
   The `bgutil-ytdlp-pot-provider` package from `requirements.txt` connects yt-dlp to it.
2. **Cookies**: send YouTube cookies with `/setcookies` (or in `/settings`). Export them from a private browser window and close it afterwards, as YouTube rotates the cookies of open sessions. A spare account is safer than your main one.

**Download order.** Every download is first tried without cookies, using YouTube clients that get no pre-roll ad. Signed-in downloads have to wait 5-6 s for the ad before YouTube serves the audio. If YouTube answers with its bot check, the bot downloads with the user's cookies instead and keeps using only cookies for `ANON_RETRY_MINUTES` (default 60).


## 🤖 Setting Up Inline Mode

1. Go to [@BotFather](https://t.me/BotFather)
2. Send `/setinline` and select your bot
3. Set the placeholder text (e.g., "Search for music...")
4. Send `/setinlinefeedback` and select your bot
5. Choose "Enabled" to receive feedback when users select results

## 📖 Usage

### Regular Commands

- **Search**: Send any song name
- **Download by link**: Send a URL from YouTube, Spotify, SoundCloud, VK, etc.
- `/settings` - Configure stats.fm and Last.fm integrations, cookies, lyrics, streaming service link buttons (YouTube, Spotify, Apple, Yandex), and Feeling lucky mode
- `/lucky` - Toggle "Feeling lucky" mode on/off
- `/now` - Download your currently playing or last played track (requires stats.fm or Last.fm)
- `/help` - Show help message

### Inline Mode

In any chat, type:
- `@botusername` - Shows your currently playing / last played track (if stats.fm or Last.fm connected)
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

The bot uses multiple strategies for accurate matching:
1. **Direct Spotify extraction**: Extracts track and artist metadata directly from Spotify
2. **YTMusic search**: Enhanced search with artist + title scoring

If you still get mismatches, the track may not be available on YouTube or may have different metadata.

### Common issues

- **"DUMP_CHAT_ID not configured"**: Add `DUMP_CHAT_ID=-100xxxxxxxxx` to your `.env` file
- **"No inline_message_id available"**: Make sure inline feedback is enabled in @BotFather
- **Download timeouts**: Large files may take longer to download - the bot has a 5-minute timeout
- **"YouTube bot check"**: see [Avoiding YouTube bot checks](#avoiding-youtube-bot-checks). If it happens with cookies set, they have probably expired: export fresh ones.

### Credits

Huge shoutout to [FallenChromium](https://github.com/FallenChromium/yam4bot) for the inline mode logic.

## 📝 License

This project is for personal use. Please respect copyright laws and terms of service of the platforms you download from.
