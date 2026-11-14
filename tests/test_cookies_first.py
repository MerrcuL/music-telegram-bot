import _isolate  # noqa: F401  (must come before `import bot`)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import yt_dlp

import bot

BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"


class DownloadPreferringCookiesTest(unittest.TestCase):
    def run_helper(self, side_effect, has_cookies, on_fallback=None):
        mock = AsyncMock(side_effect=side_effect)
        with patch.object(bot, "run_download", mock), \
                patch.object(bot, "cleanup_files", AsyncMock()):
            try:
                result = asyncio.run(bot.download_preferring_cookies(
                    1, "abc", has_cookies=has_cookies, on_fallback=on_fallback))
            except Exception as e:  # noqa: BLE001
                result = e
        return result, [c.kwargs["use_cookies"] for c in mock.call_args_list]

    def test_uses_cookies_first_when_set(self):
        result, calls = self.run_helper([{"id": "abc"}], has_cookies=True)
        self.assertEqual(result, {"id": "abc"})
        self.assertEqual(calls, [True])

    def test_no_cookies_means_one_anonymous_attempt(self):
        _, calls = self.run_helper([{"id": "abc"}], has_cookies=False)
        self.assertEqual(calls, [False])

    def test_falls_back_to_anonymous_when_cookies_fail(self):
        fallback = AsyncMock()
        result, calls = self.run_helper(
            [yt_dlp.utils.DownloadError("Requested format is not available"), {"id": "abc"}],
            has_cookies=True, on_fallback=fallback)
        self.assertEqual(result, {"id": "abc"})
        self.assertEqual(calls, [True, False])
        fallback.assert_awaited_once()

    def test_both_fail_raises_the_cookie_error(self):
        cookie_err = yt_dlp.utils.DownloadError(BOT_CHECK)
        result, calls = self.run_helper(
            [cookie_err, yt_dlp.utils.DownloadError("other")], has_cookies=True)
        self.assertIs(result, cookie_err)
        self.assertEqual(calls, [True, False])

    def test_rejections_and_timeouts_are_not_retried(self):
        for err in (bot.DownloadRejected("x", "x"), asyncio.TimeoutError()):
            result, calls = self.run_helper([err], has_cookies=True)
            self.assertIs(result, err)
            self.assertEqual(calls, [True])


class ErrorTextTest(unittest.TestCase):
    def test_bot_check_is_recognised(self):
        self.assertTrue(bot.is_bot_check_error(BOT_CHECK))
        self.assertFalse(bot.is_bot_check_error("Sign in to confirm your age"))
        self.assertIn("expired", bot.describe_download_error(Exception(BOT_CHECK), True)[0])
        self.assertIn("/setcookies", bot.describe_download_error(Exception(BOT_CHECK), False)[0])

    def test_unknown_errors_are_left_alone(self):
        self.assertIsNone(bot.describe_download_error(Exception("HTTP Error 500"), True))


class YtOptsTest(unittest.TestCase):
    def test_bgutil_base_url_reaches_the_provider(self):
        """yt-dlp must actually read the base_url the bot passes (the old list form was ignored)."""
        with yt_dlp.YoutubeDL({**bot.base_yt_opts(), 'quiet': True}) as ydl:
            ie = ydl.get_info_extractor('Youtube')
            value = ie._configuration_arg('base_url', ie_key='youtubepot-bgutilhttp', casesense=True)
        self.assertEqual(value, [bot.BGUTIL_URL])

    def test_every_option_set_carries_the_base_opts(self):
        opts = bot.get_ydl_opts(None)
        for key, value in bot.base_yt_opts().items():
            self.assertEqual(opts[key], value)


if __name__ == "__main__":
    unittest.main()
