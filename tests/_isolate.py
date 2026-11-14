"""
Imported first by every test file, before `import bot`.

Points the bot at throw-away files so running the tests can never touch the real
users_data.json / audio_cache.db, and makes `import bot` work from any directory.
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="musicbot_tests_")
os.environ["USER_SETTINGS_FILE"] = os.path.join(_TMP, "users_data.json")
os.environ["AUDIO_CACHE_DB"] = os.path.join(_TMP, "audio_cache.db")
# Tests exercise the last.fm code paths; the key itself is never sent anywhere (HTTP is mocked).
os.environ.setdefault("LASTFM_API_KEY", "test-key")
