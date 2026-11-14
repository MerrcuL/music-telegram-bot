import _isolate  # noqa: F401  (must come before `import bot`)

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import yt_dlp

import bot

BOT_CHECK = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser"


AD_FREE = ("ad-free", False)
COOKIES = ("cookies", True)
ANON = ("anonymous", False)


class DownloadWithFallbacksTest(unittest.TestCase):
    def setUp(self):
        bot._anon_blocked_until = 0.0

    def tearDown(self):
        bot._anon_blocked_until = 0.0

    def run_helper(self, side_effect, has_cookies, on_fallback=None):
        mock = AsyncMock(side_effect=side_effect)
        with patch.object(bot, "run_download", mock),                 patch.object(bot, "cleanup_files", AsyncMock()):
            try:
                result = asyncio.run(bot.download_with_fallbacks(
                    1, "abc", has_cookies=has_cookies, on_fallback=on_fallback))
            except Exception as e:  # noqa: BLE001
                result = e
        calls = []
        for c in mock.call_args_list:
            if c.kwargs.get("ad_free"):
                calls.append(AD_FREE)
            else:
                calls.append(COOKIES if c.kwargs["use_cookies"] else ANON)
        return result, calls

    def test_ad_free_anonymous_comes_first(self):
        for has_cookies in (True, False):
            result, calls = self.run_helper([{"id": "abc"}], has_cookies=has_cookies)
            self.assertEqual(result, {"id": "abc"})
            self.assertEqual(calls, [AD_FREE])

    def test_bot_check_falls_back_to_cookies_and_blocks_anonymous(self):
        fallback = AsyncMock()
        result, calls = self.run_helper(
            [yt_dlp.utils.DownloadError(BOT_CHECK), {"id": "abc"}], has_cookies=True, on_fallback=fallback)
        self.assertEqual(result, {"id": "abc"})
        self.assertEqual(calls, [AD_FREE, COOKIES])
        fallback.assert_awaited_once()
        self.assertTrue(bot.anonymous_blocked())
        # The next download goes straight to cookies.
        _, calls = self.run_helper([{"id": "abc"}], has_cookies=True)
        self.assertEqual(calls, [COOKIES])

    def test_anonymous_is_tried_again_after_the_block(self):
        self.run_helper([yt_dlp.utils.DownloadError(BOT_CHECK), {"id": "abc"}], has_cookies=True)
        bot._anon_blocked_until = bot.time.monotonic() - 1  # the hour is over
        _, calls = self.run_helper([{"id": "abc"}], has_cookies=True)
        self.assertEqual(calls, [AD_FREE])

    def test_other_errors_fall_back_without_blocking(self):
        result, calls = self.run_helper(
            [yt_dlp.utils.DownloadError("Sign in to confirm your age"), {"id": "abc"}], has_cookies=True)
        self.assertEqual(result, {"id": "abc"})
        self.assertEqual(calls, [AD_FREE, COOKIES])
        self.assertFalse(bot.anonymous_blocked())

    def test_without_cookies_other_errors_retry_default_clients(self):
        result, calls = self.run_helper(
            [yt_dlp.utils.DownloadError("Requested format is not available"), {"id": "abc"}], has_cookies=False)
        self.assertEqual(result, {"id": "abc"})
        self.assertEqual(calls, [AD_FREE, ANON])

    def test_without_cookies_a_bot_check_is_final(self):
        err = yt_dlp.utils.DownloadError(BOT_CHECK)
        result, calls = self.run_helper([err], has_cookies=False)
        self.assertIs(result, err)
        self.assertEqual(calls, [AD_FREE])
        self.assertTrue(bot.anonymous_blocked())

    def test_all_fail_raises_the_cookie_error(self):
        cookie_err = yt_dlp.utils.DownloadError("cookie attempt failed")
        result, calls = self.run_helper(
            [yt_dlp.utils.DownloadError(BOT_CHECK), cookie_err], has_cookies=True)
        self.assertIs(result, cookie_err)
        self.assertEqual(calls, [AD_FREE, COOKIES])

    def test_rejections_and_timeouts_are_not_retried(self):
        for err in (bot.DownloadRejected("x", "x"), asyncio.TimeoutError()):
            result, calls = self.run_helper([err], has_cookies=True)
            self.assertIs(result, err)
            self.assertEqual(calls, [AD_FREE])


class AdFreeOptionsTest(unittest.TestCase):
    def test_ad_free_sets_the_clients_and_keeps_bgutil(self):
        opts = bot.get_ydl_opts(None, use_cookies=False, ad_free=True)
        self.assertEqual(opts["extractor_args"]["youtube"]["player_client"], bot.AD_FREE_CLIENTS)
        self.assertIn("youtubepot-bgutilhttp", opts["extractor_args"])
        self.assertNotIn("youtube", bot.get_ydl_opts(None, use_cookies=False)["extractor_args"])


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
