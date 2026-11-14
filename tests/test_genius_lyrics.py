import os
import re
import io
import unittest
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TPE2, TDRC, USLT, TCOM, TEXT

import _isolate  # noqa: F401  (must come before `import bot`)
import bot

SAMPLE_GENIUS_TOKEN = "uswWcxjXdU64stwwtsANzNNkam9HdzhWcJ0DR31IAB2_3O3lD7iPW98-iTJhP2bd"


class TestGeniusLyricsIntegration(unittest.TestCase):
    def test_clean_title_for_search(self):
        cases = [
            ("Song Title (Official Music Video)", "Song Title"),
            ("Track Name [Official Video]", "Track Name"),
            ("Artist - Beat (Visualizer)", "Artist - Beat"),
            ("Title (Audio)", "Title"),
            ("Hit [Lyric Video]", "Hit"),
            ("Remastered 2021 (Remastered)", "Remastered 2021"),
            ("Normal Title", "Normal Title"),
        ]
        for inp, expected in cases:
            self.assertEqual(bot.clean_title_for_search(inp), expected)

    def test_clean_lyrics_text(self):
        raw_genius = (
            "[Текст песни «ты че обиделась»]\n\n"
            "[Куплет 1]\n"
            "Line 1\n"
            "Line 2\n"
            "You might also like\n"
            "123Embed"
        )
        cleaned = bot.clean_lyrics_text(raw_genius)
        self.assertNotIn("Текст песни", cleaned)
        self.assertNotIn("Embed", cleaned)
        self.assertNotIn("You might also like", cleaned)
        self.assertIn("Line 1", cleaned)
        self.assertIn("Line 2", cleaned)

    def test_is_romanized_or_translation(self):
        # Romanizations
        self.assertTrue(bot.is_romanized_or_translation("FAKE LOVE (Romanized)", "BTS", "Genius Romanizations"))
        self.assertTrue(bot.is_romanized_or_translation("Lemon", "米津玄師", "Genius Romanizations"))
        self.assertTrue(bot.is_romanized_or_translation("Положение (Романизация)", "Скриптонит", "Скриптонит"))
        
        # Translations
        self.assertTrue(bot.is_romanized_or_translation("FAKE LOVE (English Translation)", "Genius English Translations", "Genius English Translations"))
        self.assertTrue(bot.is_romanized_or_translation("Положение (Position) [Türkçe Çeviri]", "Genius Türkçe Çeviriler", "Genius Türkçe Çeviriler"))
        
        # Valid original tracks
        self.assertFalse(bot.is_romanized_or_translation("FAKE LOVE", "BTS", "BTS"))
        self.assertFalse(bot.is_romanized_or_translation("Lemon", "米津玄師 (Kenshi Yonezu)", "米津玄師 (Kenshi Yonezu)"))
        self.assertFalse(bot.is_romanized_or_translation("Положение (Position)", "Скриптонит (Skryptonite)", "Скриптонит (Skryptonite)"))
        self.assertFalse(bot.is_romanized_or_translation("ты че обиделась (are you offended)", "madk1d & тёмный принц", "madk1d"))

    def test_lrclib_lyrics_fetch(self):
        lyrics = bot.fetch_lrclib_lyrics("madk1d", "ты че обиделась")
        self.assertIsNotNone(lyrics)
        self.assertIn("обиделась", lyrics.lower())

    def test_clean_genius_title(self):
        cases = [
            ("ты че обиделась (are you offended)", "ты че обиделась"),
            ("Положение (Position)", "Положение"),
            ("Город под подошвой (City Under the Sole)", "Город под подошвой"),
            ("Положение (Position) (Remix)", "Положение (Remix)"),
            ("Положение (feat. Скриптонит)", "Положение (feat. Скриптонит)"),
            ("ты че обиделась (feat. darkprinceee)", "ты че обиделась (feat. darkprinceee)"),
            ("Bohemian Rhapsody", "Bohemian Rhapsody"),
            ("Hello (Live)", "Hello (Live)"),
            ("피 땀 눈물 (Blood Sweat & Tears)", "피 땀 눈물"),
            ("こころにくだもの [Heart’s Fruit]", "こころにくだもの"),
        ]
        for inp, expected in cases:
            self.assertEqual(bot.clean_genius_title(inp), expected)

    def test_genius_matching_and_data_fetch(self):
        data = bot.fetch_genius_data("madk1d", "ты че обиделась")
        if not bot.GENIUS_ACCESS_TOKEN:
            # Test directly with sample token
            old_token = bot.GENIUS_ACCESS_TOKEN
            bot.GENIUS_ACCESS_TOKEN = SAMPLE_GENIUS_TOKEN
            data = bot.fetch_genius_data("madk1d", "ты че обиделась")
            bot.GENIUS_ACCESS_TOKEN = old_token

        self.assertTrue(data.get("matched"))
        self.assertEqual(data.get("title"), "ты че обиделась")
        self.assertNotIn("are you offended", data.get("title"))
        self.assertIsNotNone(data.get("cover_url"))
        self.assertTrue(data.get("cover_url").startswith("http"))
        self.assertEqual(data.get("apple_music_id"), "1801530387")
        self.assertIsNotNone(data.get("album"))
        self.assertIsNotNone(data.get("lyrics"))
        self.assertGreater(len(data.get("lyrics")), 100)
        self.assertIsNotNone(data.get("embed_lyrics"))
        self.assertGreater(len(data.get("embed_lyrics")), 50)
        self.assertIsNotNone(data.get("lrclib_lyrics"))


    def test_id3_metadata_embedding(self):
        # Create a tiny dummy mp3 file
        dummy_mp3 = os.path.join(os.path.dirname(__file__), "test_temp.mp3")
        try:
            # Copy placeholder.mp3 if exists or write minimal ID3
            placeholder = os.path.join(os.path.dirname(__file__), "..", "placeholder.mp3")
            with open(placeholder, "rb") as f:
                data = f.read()
            with open(dummy_mp3, "wb") as f:
                f.write(data)

            bot._prepare_and_embed_metadata(
                dummy_mp3,
                title="Test Track",
                artist="Test Artist",
                album="Test Album",
                release_date="2025-03-14",
                lyrics="[Verse 1]\nSample Lyrics Here\nLine 2",
                composer="Producer 1, Producer 2",
                lyricist="Writer 1",
            )

            tags = ID3(dummy_mp3)
            self.assertEqual(str(tags.get("TIT2")), "Test Track")
            self.assertEqual(str(tags.get("TPE1")), "Test Artist")
            self.assertEqual(str(tags.get("TALB")), "Test Album")
            self.assertEqual(str(tags.get("TDRC")), "2025-03-14")
            self.assertEqual(str(tags.get("TCOM")), "Producer 1, Producer 2")
            self.assertEqual(str(tags.get("TEXT")), "Writer 1")
            uslt_frames = tags.getall("USLT")
            self.assertEqual(len(uslt_frames), 1)
            self.assertIn("Sample Lyrics Here", uslt_frames[0].text)

        finally:
            if os.path.exists(dummy_mp3):
                os.remove(dummy_mp3)

    def test_expandable_blockquote_format(self):
        art = "madk1d"
        tit = "ты че обиделась"
        lyrics = "[Куплет 1]\nLine 1\nLine 2"
        clean_art = bot.clean_genius_title(art)
        clean_tit = bot.clean_genius_title(tit)
        header = f"<b>{bot.html.escape(clean_art)} — {bot.html.escape(clean_tit)}</b>\n\n"
        full = f"{header}<blockquote expandable>{bot.html.escape(lyrics)}</blockquote>"
        self.assertIn("<blockquote expandable>", full)
        self.assertIn("</blockquote>", full)
        self.assertTrue(full.startswith("<b>madk1d — ты че обиделась</b>\n\n<blockquote expandable>"))

    def test_settings_toggle_lyrics(self):
        test_uid = 999999999
        original = bot.get_user_setting(test_uid, "include_lyrics", False)
        bot.set_user_setting(test_uid, "include_lyrics", not original)
        self.assertEqual(bot.get_user_setting(test_uid, "include_lyrics"), not original)
        # Restore
        bot.set_user_setting(test_uid, "include_lyrics", original)

    def test_genius_cover_and_artwork_pair_priority(self):
        old_tok = bot.GENIUS_ACCESS_TOKEN
        bot.GENIUS_ACCESS_TOKEN = SAMPLE_GENIUS_TOKEN
        try:
            cover_url = bot.fetch_genius_cover("madk1d", "ты че обиделась")
            self.assertIsNotNone(cover_url)
            self.assertTrue(cover_url.startswith("http"))

            # Test _get_artwork_pair defaults to Genius
            loop = bot.asyncio.new_event_loop()
            try:
                embed_bytes, tg_thumb_bytes = loop.run_until_complete(
                    bot._get_artwork_pair("fake_vid", artist="madk1d", track="ты че обиделась")
                )
                self.assertIsNotNone(embed_bytes)
                self.assertIsNotNone(tg_thumb_bytes)
                self.assertLess(len(tg_thumb_bytes), 200000)
            finally:
                loop.close()
        finally:
            bot.GENIUS_ACCESS_TOKEN = old_tok


if __name__ == "__main__":
    unittest.main()
