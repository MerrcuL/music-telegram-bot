# 🎵 Music Telegram Bot

A private Telegram bot that searches for songs on **YouTube Music** and **YouTube**, downloads them, and sends MP3 files directly to you. Also supports downloading by link from **Spotify, SoundCloud, VK Music, Yandex Music**, and more.


## Features

Bot uses whitelist-based access to prevent unauthorized use and avoid copyright issues.

Downloads from YouTube and YouTube Music are done with help of yt-dlp, and others are resolved with song.link and then also downloaded from YouTube. 


## Requirements

- Python 3.10+
- `ffmpeg` installed on your system
- A Telegram Bot Token (from [@BotFather](https://t.me/BotFather))


## Installation

**1. Clone the repository**

```bash
git clone https://github.com/MerrcuL/music-telegram-bot.git
cd music-telegram-bot
```

**2. Create and activate a virtual environment**

```bash
python3 -m venv venv
source venv/bin/activate
```

**3. Install Python dependencies**

```bash
pip install python-telegram-bot ytmusicapi yt-dlp python-dotenv cachetools
```

**4. Install FFmpeg**

On Debian/Ubuntu:
```bash
sudo apt install ffmpeg
```

On macOS (with Homebrew):
```bash
brew install ffmpeg
```

**5. Create a `.env` file**

```env
TOKEN=your_telegram_bot_token_here
ALLOWED_USERS=123456789,987654321
```

- `TOKEN` — your bot token from BotFather
- `ALLOWED_USERS` — comma-separated list of Telegram user IDs allowed to use the bot

> You can find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).



## Running the Bot

### Option A — Manual (foreground)

```bash
source venv/bin/activate
python bot.py
```

### Option B — systemd service (recommended for servers)

This runs the bot as a background service that starts automatically on boot and restarts on failure.

**1. Edit the service file**

Open `music-bot.service` and adjust the `User` and paths if your username or install directory differs from the defaults:

```ini
[Service]
User=anton                                          # your Linux username
WorkingDirectory=/home/anton/music-bot              # path to the repo
ExecStart=/home/anton/music-bot/venv/bin/python /home/anton/music-bot/bot.py
```

**2. Install and enable the service**

```bash
sudo cp music-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable music-bot
sudo systemctl start music-bot
```

**3. Check that it's running**

```bash
sudo systemctl status music-bot
```

**Common service commands:**

| Action | Command |
|---|---|
| Start | `sudo systemctl start music-bot` |
| Stop | `sudo systemctl stop music-bot` |
| Restart | `sudo systemctl restart music-bot` |
| View logs | `sudo journalctl -u music-bot -f` |
| Disable autostart | `sudo systemctl disable music-bot` |


## Usage

**In Telegram:**

| Action               | How                                            |
| -------------------- | ---------------------------------------------- |
| Get started          | Send `/start`                                  |
| Show help            | Send `/help`                                   |
| Search for a song    | Type a song name, e.g. `Numb Linkin Park`      |
| Download by link     | Paste any supported URL (see below)            |
| Pick a search result | Tap a number button (1–5)                      |
| Browse result pages  | Use the ⬅️ / ➡️ buttons                        |
| Cancel a search      | Tap the ❌ Cancel inline button                 |
| Cancel a download    | Tap the **🚫 Cancel Download** keyboard button |


## Supported Link Platforms

| Platform                | Method                                                |
| ----------------------- | ----------------------------------------------------- |
| YouTube / YouTube Music | Direct (yt-dlp)                                       |
| SoundCloud              | Direct (yt-dlp)                                       |
| VK Music                | Direct (yt-dlp)                                       |
| Spotify                 | Resolved via [song.link](https://song.link) → YouTube |
| Yandex Music            | Resolved via [song.link](https://song.link) → YouTube |
| Apple Music             | Resolved via [song.link](https://song.link) → YouTube |
| Tidal                   | Resolved via [song.link](https://song.link) → YouTube |
| Deezer                  | Resolved via [song.link](https://song.link) → YouTube |
| song.link / odesli.co   | Resolved via [song.link](https://song.link) → YouTube |

> Links starting with `http://` or `https://` are auto-detected — no special command needed.


