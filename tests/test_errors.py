import unittest

from core.errors import ErrorKind, classify_error, friendly_error

TECHNICAL_WORDS = ("yt-dlp", "ffmpeg", "ffprobe", "extract", "http", "errno")

CASES = {
    ErrorKind.NO_NETWORK: [
        "ERROR: [youtube] dQw4w9WgXcQ: Unable to download webpage: <urlopen error "
        "[Errno 11001] getaddrinfo failed> (caused by TransportError(...))",
        "ERROR: Unable to download API page: [WinError 10060] A connection attempt failed",
        "ERROR: Failed to resolve 'www.youtube.com' ([Errno 11001] getaddrinfo failed)",
        "ERROR: Read timed out.",
    ],
    ErrorKind.UNAVAILABLE: [
        "ERROR: [youtube] abcdefghijk: Video unavailable",
        "ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] abcdefghijk: Video unavailable. This video has been removed by the uploader",
        "ERROR: [youtube] abcdefghijk: The uploader has not made this video available in your country",
        "ERROR: [youtube] abcdefghijk: This video is not available",
    ],
    ErrorKind.SIGN_IN: [
        "ERROR: [youtube] abcdefghijk: Sign in to confirm you're not a bot. Use --cookies-from-browser "
        "or --cookies for the authentication.",
        "ERROR: [youtube] abcdefghijk: Sign in to confirm your age. This video may be inappropriate",
        "ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests",
    ],
    ErrorKind.CONVERTER_MISSING: [
        "ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path "
        "using --ffmpeg-location",
        "ERROR: ffmpeg not found. Please install or provide the path using --ffmpeg-location",
    ],
    ErrorKind.DISK_FULL: [
        "ERROR: unable to write data: [Errno 28] No space left on device",
        "ERROR: [WinError 112] There is not enough space on the disk",
    ],
    ErrorKind.NO_PERMISSION: [
        "ERROR: unable to open for writing: [Errno 13] Permission denied: 'C:\\Music\\a.mp3'",
        "ERROR: [WinError 5] Access is denied: 'C:\\Music\\a.part'",
        "ERROR: [WinError 32] The process cannot access the file because it is being used by "
        "another process",
    ],
    ErrorKind.UNKNOWN: [
        "",
        None,
        "ERROR: [youtube] abcdefghijk: Requested format is not available",
        "Something completely unexpected",
    ],
}


class FriendlyErrorTests(unittest.TestCase):
    def test_categories(self):
        for kind, samples in CASES.items():
            for raw in samples:
                with self.subTest(kind=kind, raw=raw):
                    self.assertEqual(classify_error(raw).kind, kind)

    def test_messages_are_plain_and_distinct(self):
        seen = set()
        for kind, samples in CASES.items():
            message, hint = friendly_error(samples[0])
            with self.subTest(kind=kind):
                self.assertTrue(message and hint)
                for word in TECHNICAL_WORDS:
                    self.assertNotIn(word, (message + " " + hint).lower())
                self.assertNotIn(message, seen)
                seen.add(message)

    def test_sign_in_hint_points_to_settings(self):
        _, hint = friendly_error(CASES[ErrorKind.SIGN_IN][0])
        self.assertIn("cookies in Settings", hint)

    def test_raw_text_kept_as_details_without_color_codes(self):
        raw = "\x1b[0;31mERROR:\x1b[0m [youtube] abcdefghijk: Video unavailable\n"
        error = classify_error(raw)
        self.assertEqual(error.details, "ERROR: [youtube] abcdefghijk: Video unavailable")
        self.assertEqual(error.kind, ErrorKind.UNAVAILABLE)

    def test_friendly_error_returns_tuple(self):
        result = friendly_error("Video unavailable")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
