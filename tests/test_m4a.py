import _isolate  # noqa: F401  (must come before `import bot`)

import asyncio
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import bot


def _jpeg(color=(200, 30, 30), size=(64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is needed to make a sample .m4a")
class M4aTagsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "abc.m4a")
        # One second of silence in AAC, with a description the way yt-dlp writes one.
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-t", "1", "-c:a", "aac", "-metadata", "description=whole video description",
             "-metadata", "comment=junk", self.path],
            check=True,
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_field_is_written(self):
        from mutagen.mp4 import MP4, MP4Cover

        cover = _jpeg()
        bot._prepare_and_embed_metadata(
            self.path, title="Song", artist="Artist", cover_bytes=cover, album="Album",
            album_artist="Album Artist", release_date="2024-05-01", lyrics="line 1\nline 2",
            composer="Composer", lyricist="Lyricist",
        )
        tags = MP4(self.path).tags
        self.assertEqual(tags["\xa9nam"], ["Song"])
        self.assertEqual(tags["\xa9ART"], ["Artist"])
        self.assertEqual(tags["\xa9alb"], ["Album"])
        self.assertEqual(tags["aART"], ["Album Artist"])
        self.assertEqual(tags["\xa9day"], ["2024-05-01"])
        self.assertEqual(tags["\xa9wrt"], ["Composer"])
        self.assertEqual(tags["\xa9lyr"], ["line 1\nline 2"])
        self.assertEqual(bytes(tags["----:com.apple.iTunes:LYRICIST"][0]), b"Lyricist")
        self.assertEqual(bytes(tags["covr"][0]), cover)
        self.assertEqual(tags["covr"][0].imageformat, MP4Cover.FORMAT_JPEG)
        # The video description is not music metadata.
        self.assertNotIn("desc", tags)
        self.assertNotIn("\xa9cmt", tags)

    def test_missing_fields_are_left_out(self):
        from mutagen.mp4 import MP4

        bot._prepare_and_embed_metadata(self.path, title="Only title")
        tags = MP4(self.path).tags
        self.assertEqual(tags["\xa9nam"], ["Only title"])
        for key in ("\xa9alb", "\xa9lyr", "covr", "----:com.apple.iTunes:LYRICIST"):
            self.assertNotIn(key, tags)

    def test_broken_file_does_not_raise(self):
        bad = os.path.join(self.dir, "bad.m4a")
        with open(bad, "wb") as f:
            f.write(b"not an mp4")
        bot._prepare_and_embed_metadata(bad, title="x")  # logs a warning only


class FormatOptionsTest(unittest.TestCase):
    def opts(self, fmt, cookies=False):
        with patch.object(bot, "AUDIO_FORMAT", fmt), \
                patch.object(bot, "_ensure_cookies_file", return_value="c.txt" if cookies else None):
            return bot.get_ydl_opts(1, use_cookies=cookies)

    def test_m4a_keeps_youtube_aac(self):
        o = self.opts("m4a")
        self.assertTrue(o["format"].startswith("bestaudio[ext=m4a]"))
        self.assertEqual(o["postprocessors"][0]["preferredcodec"], "m4a")
        self.assertEqual(o["postprocessor_args"], {})
        self.assertTrue(self.opts("m4a", cookies=True)["format"].startswith("bestaudio[ext=m4a]"))

    def test_mp3_uses_fast_lame(self):
        o = self.opts("mp3")
        self.assertEqual(o["postprocessors"][0]["preferredcodec"], "mp3")
        self.assertEqual(o["postprocessor_args"], {"extractaudio+ffmpeg_o": ["-compression_level", "7"]})

    def test_no_thumbnail_download(self):
        for fmt in ("m4a", "mp3", "opus"):
            o = self.opts(fmt)
            self.assertFalse(o.get("writethumbnail"))
            self.assertNotIn("FFmpegThumbnailsConvertor", [p["key"] for p in o["postprocessors"]])


class ThumbnailFallbackTest(unittest.TestCase):
    def pair(self, fetched, **kw):
        calls = []

        def fake_fetch(url, timeout=8):
            calls.append(url)
            return fetched.get(url)

        with patch.object(bot, "_fetch_artwork_from_url", fake_fetch), \
                patch.object(bot, "fetch_genius_cover", return_value=None), \
                patch.object(bot, "_read_local_thumbnail", return_value=None):
            result = asyncio.run(bot._get_artwork_pair("vid", artist="a", track="t", **kw))
        return result, calls

    def test_thumbnail_url_is_the_last_fallback(self):
        (embed, tg), calls = self.pair({"https://i.ytimg.com/x.jpg": _jpeg()},
                                       thumbnail_url="https://i.ytimg.com/x.jpg")
        self.assertIsNotNone(embed)
        self.assertIsNotNone(tg)
        self.assertEqual(calls, ["https://i.ytimg.com/x.jpg"])

    def test_thumbnail_not_fetched_when_cover_found(self):
        (embed, _), calls = self.pair({"https://cover/c.jpg": _jpeg()},
                                      cover_url="https://cover/c.jpg",
                                      thumbnail_url="https://i.ytimg.com/x.jpg")
        self.assertIsNotNone(embed)
        self.assertNotIn("https://i.ytimg.com/x.jpg", calls)


if __name__ == "__main__":
    unittest.main()
