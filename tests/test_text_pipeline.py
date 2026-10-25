"""Unit tests for the parts of the app that need no voice model.

Run from the repository root:   python -m unittest discover -s tests -v
"""
import collections
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kokoro_app as ka  # noqa: E402

Timing = collections.namedtuple("Timing", "phoneme start end")


class OptionsTestCase(unittest.TestCase):
    """Restores the text options after every test, because they are module-level settings."""

    def setUp(self):
        self._saved = dict(ka.TEXT_OPTIONS)

    def tearDown(self):
        ka.TEXT_OPTIONS.update(self._saved)


class PlainTextTests(OptionsTestCase):
    def test_dividers_headings_and_emphasis_are_not_read(self):
        out = ka.plain_clean("# Title\n\n---\n\n* * *\n\n- item one\n\nSome **bold** words.")
        self.assertIn("Title.", out)
        self.assertIn("item one.", out)
        self.assertNotIn("---", out)
        self.assertNotIn("* * *", out)
        self.assertNotIn("**", out)

    def test_urls_become_link_or_are_removed(self):
        self.assertEqual(ka.normalize_chars("see https://a.com/x. now"), "see link. now")
        ka.TEXT_OPTIONS["links"] = "drop"
        self.assertEqual(ka.normalize_chars("see https://a.com/x. now"), "see . now")

    def test_less_than_is_spoken(self):
        self.assertIn("less than", ka.normalize_chars("p < 0.05"))

    def test_symbol_only_paragraphs_are_not_chunks(self):
        chunks = ka.split_chunks("Hello there.\n\n---\n\nBye now.")
        self.assertEqual([c for c, _ in chunks], ["Hello there.", "Bye now."])
        self.assertTrue(all(pause for _, pause in chunks))  # each paragraph ends with a pause

    def test_long_sentences_are_split_under_the_limit(self):
        chunks = ka.split_chunks("word " * 300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= ka.MAX_CHUNK for c, _ in chunks))


class MarkdownTests(OptionsTestCase):
    MD = "# Heading\n\nSome text.\n\n```python\nprint('x')\n```\n\nLast line."

    def test_code_block_is_announced_or_skipped(self):
        self.assertIn("Code block skipped.", ka.markdown_to_text(self.MD))
        ka.TEXT_OPTIONS["code"] = "skip"
        text = ka.markdown_to_text(self.MD)
        self.assertNotIn("Code block skipped.", text)
        self.assertNotIn("print", text)

    def test_sections_ignore_hashes_inside_code_fences(self):
        titles = [t for t, _ in ka.split_sections("# One\n\ntext\n\n```\n# not a heading\n```\n\n# Two\n\nmore")]
        self.assertEqual(titles, ["One", "Two"])

    def test_front_matter_is_dropped(self):
        text = ka.markdown_to_text("---\ntitle: X\n---\n\n# Real\n\nBody.")
        self.assertNotIn("title", text)
        self.assertIn("Body.", text)


class PdfCleanupTests(OptionsTestCase):
    LINE = "This line is long enough to count as ordinary prose from a page of a long academic document."

    def _document(self):
        pages = [
            "Title page text\n\ni",
            "Abstract\n\n" + self.LINE + "\n\nii",
            "Contents\n\nChapter 1. Intro ............ 1\n\niii",
            "Chapter 1. Intro\n\n" + self.LINE + "\n\nsecond line of that sentence ends here.\n\n1",
            "Conclusion\n\n" + self.LINE + "\n\n2",
            "References\n\nSomeone, A. (2020). A paper.\n\n3",
        ]
        return "\x0c".join(pages)

    def test_page_numbers_and_references_are_removed(self):
        text, log = ka.prepare_pdf_markdown(self._document())
        self.assertIn("# Chapter 1. Intro", text)
        self.assertIn("# Abstract", text)
        self.assertNotIn("Someone, A.", text)
        self.assertNotIn("Contents", text)
        self.assertTrue(any("page numbers removed" in line for line in log))
        self.assertEqual([p for p in text.split("\n\n") if p.strip().isdigit()], [])

    def test_hard_wrapped_lines_are_joined_into_a_paragraph(self):
        text, _ = ka.prepare_pdf_markdown(self._document())
        self.assertIn("ordinary prose from a page of a long academic document. second line of that sentence ends here.", text)


class TimingTests(unittest.TestCase):
    def test_sentences_and_words_get_real_times(self):
        t = [Timing("h", 0.0, 0.1), Timing("i", 0.1, 0.2), Timing(" ", 0.2, 0.25),
             Timing("t", 0.25, 0.3), Timing("e", 0.3, 0.4), Timing(".", 0.4, 0.45), Timing(" ", 0.45, 0.5),
             Timing("b", 0.5, 0.6), Timing(".", 0.6, 0.65)]
        segs = ka.build_segments("Hi there. Bye.", t, 10.0, 0.7, True)
        self.assertEqual([s["t"] for s in segs], ["Hi there.", "Bye."])
        self.assertAlmostEqual(segs[0]["s"], 10.0)
        self.assertAlmostEqual(segs[0]["e"], 10.45)
        self.assertEqual([w[0] for w in segs[0]["w"]], ["Hi", "there."])
        self.assertAlmostEqual(segs[1]["s"], 10.5)
        self.assertFalse(segs[0]["p"])
        self.assertTrue(segs[1]["p"])

    def test_mismatched_sentence_count_falls_back_to_one_segment(self):
        t = [Timing("a", 0.0, 0.1), Timing(".", 0.1, 0.15)]
        segs = ka.build_segments("One. Two.", t, 0.0, 1.0, False)
        self.assertEqual(len(segs), 1)

    def test_srt_time_and_cues(self):
        self.assertEqual(ka.srt_time(3661.5), "01:01:01,500")
        srt = ka.build_srt([{"t": "Hello.", "s": 0.0, "e": 1.5, "w": None, "p": True}])
        self.assertIn("1\n00:00:00,000 --> 00:00:01,500\nHello.", srt)

    def test_chapter_title_spoken_as_two_sentences_is_found(self):
        segs = [{"t": "Chapter 1.", "s": 1.0}, {"t": "Introduction.", "s": 2.0}, {"t": "Text.", "s": 3.0}]
        self.assertEqual(ka.find_chapters(["Chapter 1. Introduction"], segs), [{"title": "Chapter 1. Introduction", "start": 1.0}])

    def test_spoken_text_is_found_in_the_original(self):
        source = "# Heading\n\nSome *quoted* text here. Then more."
        spans = ka.align_segments(source, [{"t": "Heading."}, {"t": "Some quoted text here."}, {"t": "Code block skipped."}])
        self.assertIsNotNone(spans[0])
        self.assertIsNotNone(spans[1])
        self.assertIsNone(spans[2])


class FileTests(unittest.TestCase):
    def test_encodings_and_binary_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            utf16 = os.path.join(tmp, "a.txt")
            with open(utf16, "wb") as fh:
                fh.write("﻿Hello from Notepad".encode("utf-16-le"))
            self.assertEqual(ka.read_text_file(utf16), "Hello from Notepad")
            binary = os.path.join(tmp, "b.txt")
            with open(binary, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256)) * 4)
            with self.assertRaises(ValueError):
                ka.read_text_file(binary)

    def test_unique_path_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ka.unique_path(tmp, "book")
            open(first, "w").close()
            self.assertNotEqual(ka.unique_path(tmp, "book"), first)

    def test_sync_file_round_trip_keeps_chapters_and_bookmarks(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = os.path.join(tmp, "x.mp3")
            segs = [{"t": "Hi.", "s": 0.0, "e": 0.5, "w": None, "p": True}]
            ka.write_sidecars(mp3, segs, 0.5, "af_heart", "# Hi", chapters=[{"title": "Hi", "start": 0.0}])
            data = ka.load_sidecar(mp3)
            self.assertEqual(data["chapters"], [{"title": "Hi", "start": 0.0}])
            self.assertEqual(data["bookmarks"], [])
            self.assertTrue(os.path.isfile(os.path.join(tmp, "x.srt")))

    def test_voice_names_and_estimates(self):
        self.assertEqual(ka.voice_info("bm_george"), ("George", "English (UK)", "Male"))
        words, audio, wait = ka.estimate("word " * 220, 1.0)
        self.assertEqual(words, 220)
        self.assertAlmostEqual(audio, 100.0)
        self.assertLess(wait, audio)

    def test_own_model_files_win_and_a_missing_file_falls_back(self):
        saved = dict(ka.CUSTOM_MODEL)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                model, voices = os.path.join(tmp, "m.onnx"), os.path.join(tmp, "v.bin")
                open(model, "w").close()
                open(voices, "w").close()
                ka.CUSTOM_MODEL.update(model=model, voices=voices)
                self.assertTrue(ka.using_custom_model())
                self.assertEqual(ka.model_paths(), (model, voices))
                os.remove(model)  # a custom file that has gone missing is ignored, the default is used
                self.assertFalse(ka.using_custom_model())
        finally:
            ka.CUSTOM_MODEL.update(saved)

    def test_model_file_table_is_complete(self):
        for name, info in ka.MODEL_FILES.items():
            self.assertTrue(info["url"].endswith(name))
            self.assertEqual(len(info["sha256"]), 64)
            self.assertGreater(info["size"], 1_000_000)


if __name__ == "__main__":
    unittest.main()
