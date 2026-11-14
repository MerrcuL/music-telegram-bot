import unittest
from unittest.mock import patch, MagicMock, AsyncMock
import json
import time

import _isolate  # noqa: F401  (must come before `import bot`)
import bot


class TestLinksMenu(unittest.TestCase):

    def setUp(self):
        # Reset user settings in memory for testing
        bot._settings_cache = {}
        bot._spotify_token_data = {"token": None, "expires_at": 0.0}
        bot._spotify_track_cache.clear()
        bot._apple_track_cache.clear()

    def test_settings_markup_has_links_button(self):
        markup = bot._build_settings_markup(12345)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        links_btn = next((b for b in buttons if b.callback_data == "settings:menu_links"), None)
        self.assertIsNotNone(links_btn)
        self.assertEqual(links_btn.text, "Links")

    def test_links_submenu_markup_and_text(self):
        # Initially all False
        text = bot._build_links_settings_text(12345)
        self.assertIn("Links Settings", text)

        markup = bot._build_links_settings_markup(12345)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        yt_btn = next(b for b in buttons if b.callback_data == "settings:toggle_link_yt")
        sp_btn = next(b for b in buttons if b.callback_data == "settings:toggle_link_sp")
        am_btn = next(b for b in buttons if b.callback_data == "settings:toggle_link_am")
        ya_btn = next(b for b in buttons if b.callback_data == "settings:toggle_link_ya")
        back_btn = next(b for b in buttons if b.callback_data == "settings:menu_main")

        self.assertEqual(yt_btn.text, "YouTube: OFF")
        self.assertEqual(sp_btn.text, "Spotify: OFF")
        self.assertEqual(am_btn.text, "Apple: OFF")
        self.assertEqual(ya_btn.text, "Yandex: OFF")
        self.assertEqual(back_btn.text, "Back")
        self.assertEqual(yt_btn.icon_custom_emoji_id, "5302999436085962212")
        self.assertEqual(ya_btn.icon_custom_emoji_id, bot.YANDEX_ICON_EMOJI_ID)

        # Now set YouTube, Spotify, and Yandex to True
        bot.set_user_setting(12345, "link_youtube", True)
        bot.set_user_setting(12345, "link_spotify", True)
        bot.set_user_setting(12345, "link_yandex", True)

        markup2 = bot._build_links_settings_markup(12345)
        buttons2 = [btn for row in markup2.inline_keyboard for btn in row]
        yt_btn2 = next(b for b in buttons2 if b.callback_data == "settings:toggle_link_yt")
        sp_btn2 = next(b for b in buttons2 if b.callback_data == "settings:toggle_link_sp")
        am_btn2 = next(b for b in buttons2 if b.callback_data == "settings:toggle_link_am")
        ya_btn2 = next(b for b in buttons2 if b.callback_data == "settings:toggle_link_ya")

        self.assertEqual(yt_btn2.text, "YouTube: ON")
        self.assertEqual(sp_btn2.text, "Spotify: ON")
        self.assertEqual(am_btn2.text, "Apple: OFF")
        self.assertEqual(ya_btn2.text, "Yandex: ON")

    @patch("urllib.request.urlopen")
    def test_spotify_token_caching(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "access_token": "test_access_token_123",
            "expires_in": 3600
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # First call fetches token
        token1 = bot.get_spotify_access_token()
        self.assertEqual(token1, "test_access_token_123")
        self.assertEqual(mock_urlopen.call_count, 1)

        # Second call immediately uses cache
        token2 = bot.get_spotify_access_token()
        self.assertEqual(token2, "test_access_token_123")
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("bot.get_spotify_access_token", return_value="dummy_token")
    @patch("urllib.request.urlopen")
    def test_search_spotify_track(self, mock_urlopen, mock_token):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "tracks": {
                "items": [{
                    "external_urls": {
                        "spotify": "https://open.spotify.com/track/dummy123"
                    }
                }]
            }
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        url = bot.search_spotify_track("Charli XCX", "Apple")
        self.assertEqual(url, "https://open.spotify.com/track/dummy123")

        # Second call uses cache
        url2 = bot.search_spotify_track("Charli XCX", "Apple")
        self.assertEqual(url2, "https://open.spotify.com/track/dummy123")
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_fetch_apple_music_link_from_metadata(self):
        # 1. Direct apple_music_url
        link = bot.fetch_apple_music_link(
            "Artist", "Track",
            metadata={"apple_music_url": "https://music.apple.com/song/9999"}
        )
        self.assertEqual(link, "https://music.apple.com/song/9999")

        # 2. apple_music_id
        link2 = bot.fetch_apple_music_link(
            "Artist", "Track",
            metadata={"apple_music_id": "8888"}
        )
        self.assertEqual(link2, "https://music.apple.com/song/8888")

    def test_get_yandex_music_search_url(self):
        # 1. Artist and track
        url = bot.get_yandex_music_search_url("Charli XCX", "Apple")
        self.assertEqual(url, "https://music.yandex.ru/search?text=Charli%20XCX%20Apple")

        # 2. Track with redundant artist prefix
        url2 = bot.get_yandex_music_search_url("Queen", "Queen - Bohemian Rhapsody")
        self.assertEqual(url2, "https://music.yandex.ru/search?text=Queen%20Bohemian%20Rhapsody")

        # 3. Only track
        url3 = bot.get_yandex_music_search_url(track="Imagine")
        self.assertEqual(url3, "https://music.yandex.ru/search?text=Imagine")

        # 4. Only artist
        url4 = bot.get_yandex_music_search_url(artist="Radiohead")
        self.assertEqual(url4, "https://music.yandex.ru/search?text=Radiohead")

        # 5. Empty
        url5 = bot.get_yandex_music_search_url()
        self.assertIsNone(url5)

    async def _async_test_build_track_links_markup(self):
        user_id = 99999

        # Case 0: All OFF
        markup0 = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple"
        )
        self.assertIsNone(markup0)

        # Case 1: Only YouTube ON
        bot.set_user_setting(user_id, "link_youtube", True)
        markup1 = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple"
        )
        self.assertIsNotNone(markup1)
        self.assertEqual(len(markup1.inline_keyboard), 1)
        self.assertEqual(len(markup1.inline_keyboard[0]), 1)
        self.assertEqual(markup1.inline_keyboard[0][0].text, "\u200b")
        self.assertEqual(markup1.inline_keyboard[0][0].icon_custom_emoji_id, "5302999436085962212")
        self.assertEqual(markup1.inline_keyboard[0][0].url, "https://music.youtube.com/watch?v=abc12345678")

        # Case 2: Spotify ON with original_url already having Spotify link
        bot.set_user_setting(user_id, "link_spotify", True)
        markup2 = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple",
            original_url="https://open.spotify.com/track/existing_spotify_id"
        )
        self.assertIsNotNone(markup2)
        btns2 = markup2.inline_keyboard[0]
        self.assertEqual(len(btns2), 2)
        self.assertEqual(btns2[0].text, "\u200b")
        self.assertEqual(btns2[1].text, "\u200b")
        self.assertEqual(btns2[1].icon_custom_emoji_id, "5303475095124062081")
        self.assertEqual(btns2[1].url, "https://open.spotify.com/track/existing_spotify_id")

        # Case 3: Spotify ON without original_url (triggers search_spotify_track)
        with patch("bot.search_spotify_track", return_value="https://open.spotify.com/track/searched_id"):
            markup3 = await bot.build_track_links_markup(
                user_id, video_id="abc12345678", artist="Charli XCX", track="Apple"
            )
            btns3 = markup3.inline_keyboard[0]
            self.assertEqual(btns3[1].text, "\u200b")
            self.assertEqual(btns3[1].url, "https://open.spotify.com/track/searched_id")

        # Case 4: Apple ON with metadata
        bot.set_user_setting(user_id, "link_apple", True)
        markup4 = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple",
            metadata={"apple_music_url": "https://music.apple.com/song/apple123"}
        )
        btns4 = markup4.inline_keyboard[0]
        self.assertEqual(len(btns4), 3)
        self.assertEqual(btns4[0].text, "\u200b")
        self.assertEqual(btns4[1].text, "\u200b")
        self.assertEqual(btns4[2].text, "\u200b")
        self.assertEqual(btns4[2].icon_custom_emoji_id, "5298597854816780205")
        self.assertEqual(btns4[2].url, "https://music.apple.com/song/apple123")

        # Case 5: is_inline=True produces 'yt', 'sf', 'ap' buttons
        markup_inline = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple",
            metadata={"apple_music_url": "https://music.apple.com/song/apple123"},
            is_inline=True
        )
        inline_btns = markup_inline.inline_keyboard[0]
        self.assertEqual(len(inline_btns), 3)
        self.assertEqual(inline_btns[0].text, "yt")
        self.assertIsNone(inline_btns[0].icon_custom_emoji_id)
        self.assertEqual(inline_btns[0].url, "https://music.youtube.com/watch?v=abc12345678")
        self.assertEqual(inline_btns[1].text, "sf")
        self.assertEqual(inline_btns[2].text, "ap")
        self.assertIsNone(inline_btns[2].icon_custom_emoji_id)
        self.assertEqual(inline_btns[2].url, "https://music.apple.com/song/apple123")

        # Case 6: is_inline=True with original_url from stats.fm directly produces 'sf'
        markup_statsfm = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple",
            original_url="https://open.spotify.com/track/7j5h2Y6h80H00v7o1a3jY1",
            is_inline=True
        )
        statsfm_btns = markup_statsfm.inline_keyboard[0]
        self.assertTrue(any(b.text == "sf" and "open.spotify.com" in b.url for b in statsfm_btns))
        self.assertTrue(any(b.text == "yt" for b in statsfm_btns))

        # Case 7: is_inline=True with metadata=None (cached audio), resolves via search
        markup_cached = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Queen", track="Bohemian Rhapsody",
            metadata=None,
            is_inline=True
        )
        cached_btns = markup_cached.inline_keyboard[0]
        self.assertTrue(any(b.text == "sf" for b in cached_btns))
        self.assertTrue(any(b.text == "yt" for b in cached_btns))
        self.assertTrue(any(b.text == "ap" for b in cached_btns))

        # Case 8: Yandex Music ON in regular mode uses custom emoji
        bot.set_user_setting(user_id, "link_yandex", True)
        markup_ya = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple"
        )
        ya_btns = markup_ya.inline_keyboard[0]
        self.assertEqual(len(ya_btns), 4)
        ya_btn = ya_btns[3]
        self.assertEqual(ya_btn.text, "\u200b")
        self.assertEqual(ya_btn.icon_custom_emoji_id, bot.YANDEX_ICON_EMOJI_ID)
        self.assertEqual(ya_btn.url, "https://music.yandex.ru/search?text=Charli%20XCX%20Apple")

        # Case 9: Yandex Music with overridden custom emoji configured
        with patch.object(bot, "YANDEX_ICON_EMOJI_ID", "1234567890"):
            markup_ya_emoji = await bot.build_track_links_markup(
                user_id, video_id="abc12345678", artist="Charli XCX", track="Apple"
            )
            ya_btn_emoji = markup_ya_emoji.inline_keyboard[0][3]
            self.assertEqual(ya_btn_emoji.text, "\u200b")
            self.assertEqual(ya_btn_emoji.icon_custom_emoji_id, "1234567890")

        # Case 10: Yandex Music in inline mode produces 'ym'
        markup_ya_inline = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple",
            is_inline=True
        )
        ya_inline_btn = markup_ya_inline.inline_keyboard[0][3]
        self.assertEqual(ya_inline_btn.text, "ym")
        self.assertIsNone(ya_inline_btn.icon_custom_emoji_id)
        self.assertEqual(ya_inline_btn.url, "https://music.yandex.ru/search?text=Charli%20XCX%20Apple")

        # Case 11: Yandex Music preserves original_url if already a yandex link
        markup_ya_orig = await bot.build_track_links_markup(
            user_id, video_id="abc12345678", artist="Charli XCX", track="Apple",
            original_url="https://music.yandex.ru/album/123/track/456"
        )
        ya_orig_btn = markup_ya_orig.inline_keyboard[0][3]
        self.assertEqual(ya_orig_btn.url, "https://music.yandex.ru/album/123/track/456")

    def test_build_track_links_markup(self):
        import asyncio
        asyncio.run(self._async_test_build_track_links_markup())

    def test_settings_markup_has_feeling_lucky_button(self):
        markup = bot._build_settings_markup(12345)
        buttons = [btn for row in markup.inline_keyboard for btn in row]
        lucky_btn = next((b for b in buttons if b.callback_data == "settings:toggle_lucky"), None)
        self.assertIsNotNone(lucky_btn)
        self.assertEqual(lucky_btn.text, "Feeling lucky: OFF")
        self.assertEqual(lucky_btn.icon_custom_emoji_id, "5766994197705921104")

        bot.set_user_setting(12345, "feeling_lucky", True)
        markup2 = bot._build_settings_markup(12345)
        buttons2 = [btn for row in markup2.inline_keyboard for btn in row]
        lucky_btn2 = next(b for b in buttons2 if b.callback_data == "settings:toggle_lucky")
        self.assertEqual(lucky_btn2.text, "Feeling lucky: ON")
        self.assertEqual(lucky_btn2.icon_custom_emoji_id, "5233619281710692734")

    def test_lucky_command(self):
        import asyncio
        user_id = 98765
        bot.ALLOWED_USERS.add(user_id)

        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        # 1. Toggle ON
        asyncio.run(bot.lucky_command(update, context))
        self.assertTrue(bot.get_user_setting(user_id, "feeling_lucky"))
        update.message.reply_text.assert_called_once()
        self.assertIn("Feeling lucky: ON", update.message.reply_text.call_args[0][0])

        # 2. Toggle OFF
        update.message.reply_text.reset_mock()
        asyncio.run(bot.lucky_command(update, context))
        self.assertFalse(bot.get_user_setting(user_id, "feeling_lucky"))
        update.message.reply_text.assert_called_once()
        self.assertIn("Feeling lucky: OFF", update.message.reply_text.call_args[0][0])

    @patch("bot.search_hybrid")
    @patch("bot._download_and_send_result_track", new_callable=AsyncMock)
    def test_handle_search_feeling_lucky(self, mock_download, mock_search):
        import asyncio
        user_id = 11111
        bot.ALLOWED_USERS.add(user_id)
        mock_search.return_value = [
            {'id': 'v1', 'title': 'Song 1', 'uploader': 'Artist 1', 'source': '🎵'},
            {'id': 'v2', 'title': 'Song 2', 'uploader': 'Artist 2', 'source': '📺'},
        ]

        update = MagicMock()
        update.effective_user.id = user_id
        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = MagicMock()

        # Feeling lucky is ON -> should download first result immediately
        bot.set_user_setting(user_id, "feeling_lucky", True)
        asyncio.run(bot.handle_search(update, context, "Song 1"))

        mock_download.assert_called_once_with(
            update, context, user_id, mock_search.return_value[0], status_msg
        )


if __name__ == "__main__":
    unittest.main()
