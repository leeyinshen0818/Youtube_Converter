import unittest

from core.urls import (
    MAX_LINKS,
    LinkError,
    SkipReason,
    extract_video_id,
    normalize_url,
    parse_links,
)

VID = "dQw4w9WgXcQ"
VID2 = "9bZkp7q19f0"
WATCH = f"https://www.youtube.com/watch?v={VID}"


def make_id(index: int) -> str:
    return f"vid{index:08d}"  # 11 characters


class NormalizeUrlTests(unittest.TestCase):
    def assert_normalizes(self, raw: str, video_id: str = VID) -> None:
        self.assertEqual(normalize_url(raw), f"https://www.youtube.com/watch?v={video_id}")

    def assert_rejects(self, raw: str, reason: SkipReason) -> None:
        with self.assertRaises(LinkError) as ctx:
            normalize_url(raw)
        self.assertEqual(ctx.exception.reason, reason)

    def test_accepted_hosts(self):
        for host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            with self.subTest(host=host):
                self.assert_normalizes(f"https://{host}/watch?v={VID}")

    def test_youtu_be(self):
        self.assert_normalizes(f"https://youtu.be/{VID}")
        self.assert_normalizes(f"https://youtu.be/{VID}?si=abcDEF123&t=42")

    def test_music_with_list_is_single_video(self):
        self.assert_normalizes(
            f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}&si=xyz"
        )

    def test_watch_with_list_is_single_video(self):
        self.assert_normalizes(
            f"https://www.youtube.com/watch?v={VID}&list=PLabcdefghijklmnop&index=3"
        )

    def test_extra_params_dropped(self):
        self.assert_normalizes(f"https://www.youtube.com/watch?app=desktop&v={VID}&t=1m5s&si=q")
        self.assert_normalizes(f"https://www.youtube.com/watch?v={VID}#comments")

    def test_http_and_missing_scheme(self):
        self.assert_normalizes(f"http://www.youtube.com/watch?v={VID}")
        self.assert_normalizes(f"www.youtube.com/watch?v={VID}")
        self.assert_normalizes(f"youtu.be/{VID}")

    def test_host_is_case_insensitive(self):
        self.assert_normalizes(f"HTTPS://WWW.YouTube.com/watch?v={VID}")

    def test_shorts_live_embed(self):
        for path in ("shorts", "live", "embed"):
            with self.subTest(path=path):
                self.assert_normalizes(f"https://www.youtube.com/{path}/{VID}?feature=share")

    def test_playlist_rejected(self):
        self.assert_rejects("https://www.youtube.com/playlist?list=PLabcdefghijk", SkipReason.PLAYLIST)
        self.assert_rejects("https://music.youtube.com/playlist?list=OLAK5uy_abc", SkipReason.PLAYLIST)
        self.assert_rejects("https://www.youtube.com/watch?list=PLabcdefghijk", SkipReason.PLAYLIST)

    def test_channel_and_handle_rejected(self):
        for raw in (
            "https://www.youtube.com/@SomeArtist",
            "https://www.youtube.com/@SomeArtist/videos",
            "https://www.youtube.com/channel/UC1234567890",
            "https://www.youtube.com/c/SomeName",
            "https://www.youtube.com/user/SomeName",
            "https://music.youtube.com/channel/UC1234567890",
        ):
            with self.subTest(raw=raw):
                self.assert_rejects(raw, SkipReason.CHANNEL)

    def test_album_browse_rejected(self):
        self.assert_rejects("https://music.youtube.com/browse/MPREb_abcdefghijk", SkipReason.ALBUM)

    def test_not_youtube(self):
        self.assert_rejects("https://vimeo.com/12345", SkipReason.NOT_YOUTUBE)
        self.assert_rejects("https://youtube.com.evil.example/watch?v=" + VID, SkipReason.NOT_YOUTUBE)

    def test_invalid(self):
        for raw in (
            "hello",
            "ftp://www.youtube.com/watch?v=" + VID,
            "https://www.youtube.com/",
            "https://www.youtube.com/watch",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/results?search_query=song",
            "https://youtu.be/",
            "https://youtu.be/tooShort",
            "https://www.youtube.com/shorts/",
        ):
            with self.subTest(raw=raw):
                self.assert_rejects(raw, SkipReason.INVALID)

    def test_extract_video_id(self):
        self.assertEqual(extract_video_id(f"https://youtu.be/{VID}"), VID)


class ParseLinksTests(unittest.TestCase):
    def test_empty_input(self):
        result = parse_links("  \n\n ")
        self.assertEqual(result.valid, ())
        self.assertEqual(result.skipped, ())

    def test_lines_and_whitespace_split(self):
        text = f"https://youtu.be/{VID}\n\n  https://www.youtube.com/watch?v={VID2}\tfoo\r\n"
        result = parse_links(text)
        self.assertEqual(result.urls, [WATCH, f"https://www.youtube.com/watch?v={VID2}"])
        self.assertEqual(len(result.skipped), 1)
        self.assertEqual(result.skipped[0].reason, SkipReason.INVALID)
        self.assertEqual(result.skipped[0].span.text, "foo")

    def test_spans_point_at_original_text(self):
        text = f"bad-line\n  <https://youtu.be/{VID}>,\r\nhttps://vimeo.com/1"
        result = parse_links(text)
        link = result.valid[0]
        self.assertEqual(link.span.line, 1)
        self.assertEqual(text[link.span.start:link.span.end], f"https://youtu.be/{VID}")
        bad = result.skipped[0]
        self.assertEqual((bad.span.line, bad.span.start, bad.span.end), (0, 0, 8))
        vimeo = result.skipped[1]
        self.assertEqual(vimeo.span.line, 2)
        self.assertEqual(vimeo.reason, SkipReason.NOT_YOUTUBE)
        self.assertEqual(text[vimeo.span.start:vimeo.span.end], "https://vimeo.com/1")

    def test_bad_lines_mixed_with_valid(self):
        text = "\n".join([
            WATCH,
            "not a link",
            "https://www.youtube.com/playlist?list=PLx",
            "https://www.youtube.com/@artist",
            f"https://music.youtube.com/watch?v={VID2}&list=RDAMVMx",
        ])
        result = parse_links(text)
        self.assertEqual(len(result.valid), 2)
        reasons = [item.reason for item in result.skipped]
        self.assertEqual(
            reasons,
            [SkipReason.INVALID, SkipReason.PLAYLIST, SkipReason.CHANNEL],
        )
        self.assertEqual(result.skipped[0].span.text, "not a link")
        self.assertTrue(all(item.message for item in result.skipped))
        self.assertEqual(len(result.input_errors), 3)

    def test_plain_words_on_one_line_are_one_skipped_item(self):
        text = f"my favourite song\n{WATCH}"
        result = parse_links(text)
        self.assertEqual(len(result.skipped), 1)
        bad = result.skipped[0]
        self.assertEqual(bad.span.text, "my favourite song")
        self.assertEqual(text[bad.span.start:bad.span.end], "my favourite song")

    def test_words_are_not_merged_across_a_link_or_lines(self):
        result = parse_links(f"intro {WATCH} outro\nnext line")
        self.assertEqual(len(result.valid), 1)
        self.assertEqual(
            [item.span.text for item in result.skipped], ["intro", "outro", "next line"]
        )

    def test_different_reasons_are_not_merged(self):
        result = parse_links("hello https://vimeo.com/1 world")
        self.assertEqual(
            [item.reason for item in result.skipped],
            [SkipReason.INVALID, SkipReason.NOT_YOUTUBE, SkipReason.INVALID],
        )

    def test_duplicates_by_video_id(self):
        text = "\n".join([
            WATCH,
            f"https://youtu.be/{VID}?si=abc",
            f"https://music.youtube.com/watch?v={VID}&list=RDx",
            f"https://www.youtube.com/shorts/{VID}",
        ])
        result = parse_links(text)
        self.assertEqual(result.urls, [WATCH])
        self.assertEqual([item.reason for item in result.skipped], [SkipReason.DUPLICATE] * 3)
        self.assertEqual({item.span.line for item in result.skipped}, {1, 2, 3})
        self.assertTrue(all(item.video_id == VID for item in result.skipped))
        self.assertEqual(result.input_errors, ())

    def test_over_limit_overflow(self):
        ids = [make_id(index) for index in range(60)]
        text = "\n".join(f"https://youtu.be/{video_id}" for video_id in ids)
        result = parse_links(text)
        self.assertEqual(len(result.valid), MAX_LINKS)
        self.assertEqual([link.video_id for link in result.valid], ids[:MAX_LINKS])
        self.assertEqual(len(result.skipped), 10)
        self.assertTrue(all(item.reason == SkipReason.OVER_LIMIT for item in result.skipped))
        self.assertEqual(result.skipped[0].span.line, MAX_LINKS)
        self.assertEqual(result.skipped_counts(), {SkipReason.OVER_LIMIT: 10})

    def test_duplicates_and_bad_lines_do_not_use_up_the_limit(self):
        lines = [WATCH, WATCH, "junk"] + [f"https://youtu.be/{make_id(i)}" for i in range(49)]
        result = parse_links("\n".join(lines))
        self.assertEqual(len(result.valid), MAX_LINKS)
        self.assertEqual(
            result.skipped_counts(), {SkipReason.DUPLICATE: 1, SkipReason.INVALID: 1}
        )

    def test_duplicate_after_limit_is_reported_as_duplicate(self):
        lines = [f"https://youtu.be/{make_id(i)}" for i in range(MAX_LINKS)] + [
            f"https://youtu.be/{make_id(0)}",
            f"https://youtu.be/{make_id(99)}",
        ]
        result = parse_links("\n".join(lines))
        self.assertEqual(
            [item.reason for item in result.skipped],
            [SkipReason.DUPLICATE, SkipReason.OVER_LIMIT],
        )

    def test_custom_limit(self):
        result = parse_links(f"{WATCH}\nhttps://youtu.be/{VID2}", limit=1)
        self.assertEqual(result.urls, [WATCH])
        self.assertEqual(result.skipped[0].reason, SkipReason.OVER_LIMIT)


if __name__ == "__main__":
    unittest.main()
