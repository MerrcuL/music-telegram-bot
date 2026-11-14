import unittest
import _isolate  # noqa: F401  (must come before `import bot`)
import bot


class TestAudioFilename(unittest.TestCase):
    def test_standard_format(self):
        self.assertEqual(
            bot.format_audio_filename("Queen", "Bohemian Rhapsody"),
            "Queen - Bohemian Rhapsody.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename("Daft Punk", "Get Lucky (feat. Pharrell Williams)"),
            "Daft Punk - Get Lucky (feat. Pharrell Williams).mp3"
        )

    def test_sanitization(self):
        self.assertEqual(
            bot.format_audio_filename("AC/DC", "Back In Black"),
            "AC-DC - Back In Black.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename("The Weeknd", "Starboy: Chapter 1"),
            "The Weeknd - Starboy - Chapter 1.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename("Panic! At The Disco", "Victorious?"),
            "Panic! At The Disco - Victorious.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename("Artist", "Track <Bonus> *Deluxe* | VIP"),
            "Artist - Track Bonus Deluxe VIP.mp3"
        )

    def test_missing_or_empty_values(self):
        self.assertEqual(
            bot.format_audio_filename(None, "Unknown Track"),
            "Unknown Track.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename("Artist", None),
            "Artist.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename("", ""),
            "audio.mp3"
        )
        self.assertEqual(
            bot.format_audio_filename(None, None),
            "audio.mp3"
        )


if __name__ == "__main__":
    unittest.main()
