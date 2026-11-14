import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import json
import asyncio

import _isolate  # noqa: F401  (must come before `import bot`)
import bot


class TestLastFmIntegration(unittest.TestCase):

    def setUp(self):
        bot._settings_cache = {}
        bot.AWAITING_LASTFM_USERNAME.clear()
        bot.AWAITING_STATS_FM_USERNAME.clear()

    def test_settings_text_contains_lastfm(self):
        user_id = 12345
        text = bot._build_settings_text(user_id)
        self.assertIn("stats.fm username: <code>None</code>", text)
        self.assertIn("last.fm username: <code>None</code>", text)

        bot.set_user_setting(user_id, "lastfm_username", "my_lastfm_user")
        bot.set_user_setting(user_id, "stats_fm_username", "my_statsfm_user")
        text2 = bot._build_settings_text(user_id)
        self.assertIn("stats.fm username: <code>my_statsfm_user</code>", text2)
        self.assertIn("last.fm username: <code>my_lastfm_user</code>", text2)

    def test_settings_markup_has_lastfm_button(self):
        markup = bot._build_settings_markup(12345)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        bind_lfm = next((b for b in buttons if b.callback_data == "settings:bind_lastfm"), None)
        bind_sfm = next((b for b in buttons if b.callback_data == "settings:bind_stats_fm"), None)
        self.assertIsNotNone(bind_lfm)
        self.assertIsNotNone(bind_sfm)
        self.assertEqual(bind_lfm.text, "Bind last.fm account")
        self.assertEqual(bind_lfm.icon_custom_emoji_id, "5298951425114520052")
        self.assertEqual(bind_sfm.text, "Bind stats.fm account")

    def test_settings_markup_unbind_buttons_when_set(self):
        user_id = 12345
        bot.set_user_setting(user_id, "lastfm_username", "my_lfm")
        bot.set_user_setting(user_id, "stats_fm_username", "my_sfm")

        markup = bot._build_settings_markup(user_id)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        unbind_lfm = next((b for b in buttons if b.callback_data == "settings:unbind_lastfm"), None)
        unbind_sfm = next((b for b in buttons if b.callback_data == "settings:unbind_stats_fm"), None)
        self.assertIsNotNone(unbind_lfm)
        self.assertIsNotNone(unbind_sfm)
        self.assertEqual(unbind_lfm.text, "Unbind last.fm account")
        self.assertEqual(unbind_lfm.icon_custom_emoji_id, "5298951425114520052")
        self.assertEqual(unbind_sfm.text, "Unbind stats.fm account")

    def test_unbind_lastfm_callback(self):
        user_id = 998877
        bot.ALLOWED_USERS.add(user_id)
        bot.set_user_setting(user_id, "lastfm_username", "testuser")

        update = MagicMock()
        update.effective_user.id = user_id
        update.callback_query.data = "settings:unbind_lastfm"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.handle_callback(update, context))

        self.assertIsNone(bot.get_user_setting(user_id, "lastfm_username"))
        update.callback_query.answer.assert_any_call("last.fm account unbound ✅")
        update.callback_query.edit_message_text.assert_called_once()

    def test_unbind_statsfm_callback(self):
        user_id = 998877
        bot.ALLOWED_USERS.add(user_id)
        bot.set_user_setting(user_id, "stats_fm_username", "testuser")

        update = MagicMock()
        update.effective_user.id = user_id
        update.callback_query.data = "settings:unbind_stats_fm"
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.handle_callback(update, context))

        self.assertIsNone(bot.get_user_setting(user_id, "stats_fm_username"))
        update.callback_query.answer.assert_any_call("stats.fm account unbound ✅")
        update.callback_query.edit_message_text.assert_called_once()

    def test_bind_lastfm_callback(self):
        user_id = 998877
        bot.ALLOWED_USERS.add(user_id)

        update = MagicMock()
        update.effective_user.id = user_id
        update.callback_query.data = "settings:bind_lastfm"
        update.callback_query.answer = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.handle_callback(update, context))

        self.assertIn(user_id, bot.AWAITING_LASTFM_USERNAME)
        update.callback_query.message.reply_text.assert_called_once()
        self.assertIn("last.fm username", update.callback_query.message.reply_text.call_args[0][0])

    def test_handle_message_saves_lastfm_username(self):
        user_id = 998877
        bot.ALLOWED_USERS.add(user_id)
        bot.AWAITING_LASTFM_USERNAME.add(user_id)

        update = MagicMock()
        update.effective_user.id = user_id
        update.message.text = "lastfm_fan_42"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.handle_message(update, context))

        self.assertNotIn(user_id, bot.AWAITING_LASTFM_USERNAME)
        self.assertEqual(bot.get_user_setting(user_id, "lastfm_username"), "lastfm_fan_42")
        update.message.reply_text.assert_called_once()
        self.assertIn("lastfm_fan_42", update.message.reply_text.call_args[0][0])

    def test_clean_title_for_search_strips_lastfm_prefix(self):
        self.assertEqual(
            bot.clean_title_for_search("(last.fm): Song Title"),
            "Song Title"
        )
        self.assertEqual(
            bot.clean_title_for_search("[Last.fm] Artist - Song"),
            "Artist - Song"
        )
        self.assertEqual(
            bot.clean_title_for_search("(stats.fm): Song Title"),
            "Song Title"
        )

    @patch("bot._http_get_json")
    def test_get_lastfm_tracks_info_now_playing(self, mock_http):
        user_id = 112233
        bot.set_user_setting(user_id, "lastfm_username", "testuser")

        mock_http.return_value = {
            "recenttracks": {
                "track": [
                    {
                        "name": "Paranoid Android",
                        "artist": {"#text": "Radiohead"},
                        "image": [
                            {"size": "small", "#text": "https://img/small.jpg"},
                            {"size": "extralarge", "#text": "https://img/large.jpg"}
                        ],
                        "@attr": {"nowplaying": "true"}
                    },
                    {
                        "name": "Karma Police",
                        "artist": {"#text": "Radiohead"},
                        "image": [{"size": "large", "#text": "https://img/kp.jpg"}]
                    }
                ]
            }
        }

        tracks = asyncio.run(bot.get_lastfm_tracks_info(user_id))
        self.assertEqual(len(tracks), 2)

        # Track 0: now playing
        self.assertEqual(tracks[0]["track_name"], "Paranoid Android")
        self.assertEqual(tracks[0]["artist_name"], "Radiohead")
        self.assertEqual(tracks[0]["status_text"], "(last.fm)")
        self.assertTrue(tracks[0]["is_now_playing"])
        self.assertTrue(tracks[0]["has_lastfm"])
        self.assertEqual(tracks[0]["cover_url"], "https://img/large.jpg")

        # Track 1: recent
        self.assertEqual(tracks[1]["track_name"], "Karma Police")
        self.assertEqual(tracks[1]["status_text"], "(recent)")
        self.assertFalse(tracks[1]["is_now_playing"])

    @patch("bot._http_get_json")
    def test_get_lastfm_tracks_info_single_dict(self, mock_http):
        user_id = 112233
        bot.set_user_setting(user_id, "lastfm_username", "testuser")

        mock_http.return_value = {
            "recenttracks": {
                "track": {
                    "name": "Creep",
                    "artist": {"#text": "Radiohead"},
                    "@attr": {"nowplaying": "true"}
                }
            }
        }

        tracks = asyncio.run(bot.get_lastfm_tracks_info(user_id))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["track_name"], "Creep")
        self.assertEqual(tracks[0]["status_text"], "(last.fm)")

    @patch("bot._http_get_json")
    def test_fetch_lastfm_now_playing(self, mock_http):
        mock_http.return_value = {
            "recenttracks": {
                "track": [{
                    "name": "Numb",
                    "artist": {"#text": "Linkin Park"},
                    "@attr": {"nowplaying": "true"}
                }]
            }
        }

        res = bot._fetch_lastfm_now_playing("lp_fan")
        self.assertIsNotNone(res)
        self.assertEqual(res["track_name"], "Numb")
        self.assertEqual(res["artist_name"], "Linkin Park")
        self.assertEqual(res["status_text"], "Currently playing")
        self.assertTrue(res["is_now_playing"])
        self.assertEqual(res["service"], "last.fm")

    @patch("bot._fetch_lastfm_now_playing")
    @patch("bot.search_spotify_track_on_youtube")
    @patch("bot.get_cached_audio", return_value=None)
    @patch("bot.process_download_and_upload", new_callable=AsyncMock)
    def test_now_command_with_lastfm_only(self, mock_proc, mock_cached, mock_search_yt, mock_fetch_lfm):
        user_id = 55555
        bot.ALLOWED_USERS.add(user_id)
        bot.set_user_setting(user_id, "lastfm_username", "user_lfm")

        mock_fetch_lfm.return_value = {
            "track_name": "In the End",
            "artist_name": "Linkin Park",
            "status_text": "Currently playing",
            "is_now_playing": True,
            "spotify_url": None,
            "cover_url": "https://img.jpg",
            "service": "last.fm",
        }
        mock_search_yt.return_value = {
            "id": "vid123",
            "title": "In the End",
            "uploader": "Linkin Park",
            "cover_url": "https://yt_img.jpg",
        }

        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = MagicMock()
        context.args = []

        asyncio.run(bot.now_command(update, context))

        mock_fetch_lfm.assert_called_once_with("user_lfm")
        mock_proc.assert_called_once()
        self.assertEqual(mock_proc.call_args[1]["fallback_title"], "In the End")
        self.assertEqual(mock_proc.call_args[1]["fallback_artist"], "Linkin Park")
        self.assertTrue(mock_proc.call_args[1]["authoritative"])

    @patch("bot.get_lastfm_tracks_info")
    @patch("bot.get_statsfm_tracks_info", return_value=[])
    def test_inline_query_lastfm(self, mock_sfm, mock_lfm):
        user_id = 77777
        bot.ALLOWED_USERS.add(user_id)

        mock_lfm.return_value = [{
            "track_name": "Faint",
            "artist_name": "Linkin Park",
            "search_query": "Linkin Park Faint",
            "spotify_url": None,
            "status_text": "(last.fm)",
            "has_lastfm": True,
            "cover_url": None,
            "is_now_playing": True,
        }]

        update = MagicMock()
        update.effective_user.id = user_id
        update.inline_query.query = ""
        update.inline_query.answer = AsyncMock()
        context = MagicMock()

        asyncio.run(bot.inline_query_handler(update, context))

        update.inline_query.answer.assert_called_once()
        results = update.inline_query.answer.call_args[0][0]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].id.startswith("lastfm:"))
        self.assertEqual(results[0].title, "(last.fm): Faint")
        self.assertEqual(results[0].performer, "Linkin Park")


if __name__ == "__main__":
    unittest.main()

