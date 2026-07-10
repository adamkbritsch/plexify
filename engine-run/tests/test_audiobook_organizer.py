"""Unit tests for the audiobook organizer's pure logic — no network, no real m4b files."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from app import audiobook_organizer as ab


class TestInferBookGuess(unittest.TestCase):
    def test_author_dash_title(self):
        g = ab.infer_book_guess("/x/Andy Weir - Project Hail Mary.m4b")
        self.assertEqual(g["title"], "Project Hail Mary")
        self.assertEqual(g["author"], "Andy Weir")

    def test_title_paren_author(self):
        g = ab.infer_book_guess("/x/Project Hail Mary (Andy Weir).m4b")
        self.assertEqual(g["title"], "Project Hail Mary")
        self.assertEqual(g["author"], "Andy Weir")

    def test_bare_title(self):
        g = ab.infer_book_guess("/x/Dune.m4b")
        self.assertEqual(g["title"], "Dune")
        self.assertIsNone(g["author"])

    def test_year_paren_is_not_author(self):
        g = ab.infer_book_guess("/x/Dune (1965).m4b")
        self.assertEqual(g["title"], "Dune")
        self.assertIsNone(g["author"])

    def test_junk_stripped(self):
        g = ab.infer_book_guess("/x/Frank Herbert - Dune [Unabridged 64k].m4b")
        self.assertEqual(g["title"], "Dune")
        self.assertEqual(g["author"], "Frank Herbert")

    def test_tags_win_over_filename(self):
        g = ab.infer_book_guess("/x/whatever_rip_001.m4b",
                                {"album": "The Martian", "albumartist": "Andy Weir"})
        self.assertEqual(g["title"], "The Martian")
        self.assertEqual(g["author"], "Andy Weir")

    def test_long_left_side_is_title_not_author(self):
        g = ab.infer_book_guess(
            "/x/The Hitchhikers Guide to the Galaxy Complete Radio Series - Part 1.m4b")
        # 8-word left side: too long for an author; treated as bare title parse
        self.assertIsNone(g["author"])


CANDS = [
    {"asin": "B08G9PRS1K", "title": "Project Hail Mary", "authors": ["Andy Weir"]},
    {"asin": "B002V0QK4C", "title": "The Martian", "authors": ["Andy Weir"]},
    {"asin": "B0XXXXXXXX", "title": "Project Hail Mary Summary", "authors": ["QuickReads"]},
]


class TestPickCandidate(unittest.TestCase):
    def test_accepts_close_match(self):
        best, score = ab.pick_candidate(
            {"title": "Project Hail Mary", "author": "Andy Weir"}, CANDS)
        self.assertIsNotNone(best)
        self.assertEqual(best["asin"], "B08G9PRS1K")
        self.assertGreaterEqual(score, 90)

    def test_rejects_low_confidence(self):
        best, score = ab.pick_candidate(
            {"title": "Completely Different Book", "author": "Nobody Knows"}, CANDS)
        self.assertIsNone(best)
        self.assertLess(score, 80)

    def test_author_mismatch_vetoes_perfect_title(self):
        # same title, genuinely different author (the classic audiobook mismatch)
        cands = [{"asin": "A1", "title": "Dune", "authors": ["Kevin J. Anderson"]}]
        best, _ = ab.pick_candidate({"title": "Dune", "author": "Frank Herbert"}, cands)
        self.assertIsNone(best)

    def test_author_superset_still_matches(self):
        # a candidate author string CONTAINING the real author must not be vetoed
        cands = [{"asin": "A2", "title": "Dune", "authors": ["Frank Herbert and Brian Herbert"]}]
        best, score = ab.pick_candidate({"title": "Dune", "author": "Frank Herbert"}, cands)
        self.assertIsNotNone(best)
        self.assertGreaterEqual(score, 80)

    def test_no_author_discounts_but_can_pass(self):
        best, score = ab.pick_candidate({"title": "Project Hail Mary", "author": None}, CANDS)
        self.assertIsNotNone(best)
        self.assertEqual(best["asin"], "B08G9PRS1K")

    def test_empty_candidates(self):
        best, score = ab.pick_candidate({"title": "Dune", "author": None}, [])
        self.assertIsNone(best)
        self.assertEqual(score, 0)


AUDNEXUS_FIXTURE = {
    "asin": "B08G9PRS1K",
    "title": "Project Hail Mary",
    "subtitle": "",
    "authors": ["Andy Weir"],
    "narrators": ["Ray Porter"],
    "release_date": "2021-05-04",
    "summary": "<p>Ryland Grace is the <b>sole survivor</b>.</p>",
    "image": "https://m.media-amazon.com/images/I/x.jpg",
    "genres": ["Science Fiction"],
    "series": "",
    "series_position": "",
}


class TestBuildMp4Tags(unittest.TestCase):
    def test_mapping(self):
        t = ab.build_mp4_tags(AUDNEXUS_FIXTURE)
        self.assertEqual(t["\xa9alb"], "Project Hail Mary")
        self.assertEqual(t["aART"], "Andy Weir")
        self.assertEqual(t["\xa9ART"], "Andy Weir")
        self.assertEqual(t["\xa9wrt"], "Ray Porter")
        self.assertEqual(t["\xa9day"], "2021")
        self.assertEqual(t["\xa9gen"], "Science Fiction")
        self.assertNotIn("<p>", t["desc"])          # HTML stripped
        self.assertIn("sole survivor", t["desc"])
        self.assertEqual(t["----:com.apple.iTunes:ASIN"], "B08G9PRS1K")

    def test_series_sort_album(self):
        meta = dict(AUDNEXUS_FIXTURE, series="Bobiverse", series_position="1",
                    title="We Are Legion")
        t = ab.build_mp4_tags(meta)
        self.assertEqual(t["soal"], "Bobiverse 1 - We Are Legion")


class TestDestPath(unittest.TestCase):
    def test_sanitization(self):
        meta = {"title": "Book: The? Sequel", "authors": ["A/B Author"]}
        d = ab.dest_for(meta, "/lib")
        self.assertNotIn("?", d)
        self.assertNotIn(":", d.replace("/lib", ""))
        self.assertTrue(d.endswith(".m4b"))
        self.assertTrue(d.startswith("/lib/"))

    def test_author_title_layout(self):
        d = ab.dest_for({"title": "Dune", "authors": ["Frank Herbert"]}, "/lib")
        self.assertEqual(d, "/lib/Frank Herbert/Dune/Dune.m4b")


class TestSettled(unittest.TestCase):
    def test_fresh_file_not_settled(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.m4b")
            open(p, "wb").write(b"x" * 10)
            memo = {}
            self.assertFalse(ab._settled(p, memo))       # first sight + fresh mtime

    def test_old_stable_file_settled(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.m4b")
            open(p, "wb").write(b"x" * 10)
            old = time.time() - 600
            os.utime(p, (old, old))
            memo = {}
            self.assertFalse(ab._settled(p, memo))       # first pass memoizes size
            self.assertTrue(ab._settled(p, memo))        # second pass: old + stable

    def test_growing_file_not_settled(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "a.m4b")
            open(p, "wb").write(b"x" * 10)
            old = time.time() - 600
            os.utime(p, (old, old))
            memo = {}
            ab._settled(p, memo)
            open(p, "ab").write(b"more")
            os.utime(p, (old, old))
            self.assertFalse(ab._settled(p, memo))       # size changed between passes


class TestIterUntagged(unittest.TestCase):
    def test_folder_and_bare_layouts(self):
        # auto-m4b puts each book in a FOLDER (observed live: untagged/<Book>/<Book>.m4b
        # + .chapters.txt); bare top-level m4bs must also be found.
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "Sun Tzu - The Art of War"))
            open(os.path.join(td, "Sun Tzu - The Art of War", "Sun Tzu - The Art of War.m4b"), "wb").close()
            open(os.path.join(td, "Sun Tzu - The Art of War", "Sun Tzu - The Art of War.chapters.txt"), "wb").close()
            open(os.path.join(td, "bare book.m4b"), "wb").close()
            os.makedirs(os.path.join(td, "empty folder"))
            found = ab._iter_untagged(td)
        paths = sorted(p for p, _ in found)
        self.assertEqual(len(found), 2)
        self.assertTrue(paths[0].endswith("Sun Tzu - The Art of War.m4b"))
        self.assertTrue(paths[1].endswith("bare book.m4b"))
        containers = {os.path.basename(p): c for p, c in found}
        self.assertIsNone(containers["bare book.m4b"])
        self.assertTrue(containers["Sun Tzu - The Art of War.m4b"].endswith("Sun Tzu - The Art of War"))

    def test_cleanup_book_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "Book")
            os.makedirs(d)
            open(os.path.join(d, "Book.chapters.txt"), "wb").close()
            ab._cleanup_book_dir(d)
            self.assertFalse(os.path.exists(d))     # sidecar removed + emptied dir dropped
            ab._cleanup_book_dir(None)              # no-op, no raise


class TestLedger(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ab, "DATA_DIR", td):
                ab._log_book({"status": "organized", "file": "a.m4b", "title": "Dune",
                              "author": "Frank Herbert"})
                ab._log_book({"status": "review", "file": "b.m4b", "guess": {"title": "?"}})
                recs = ab.book_records(10)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["status"], "review")     # most recent first
        self.assertEqual(recs[1]["title"], "Dune")

    def test_missing_ledger_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(ab, "DATA_DIR", td):
                self.assertEqual(ab.book_records(), [])


class TestImportRouting(unittest.TestCase):
    """The unified plexify-imports folder: audiobook-shaped items route to auto-m4b's intake,
    FLAC stays for the music pass (classification lives in import_folder)."""

    def _mk(self, td, rel):
        p = os.path.join(td, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"x")
        return p

    def test_classify(self):
        from app import import_folder as IF
        with tempfile.TemporaryDirectory() as td:
            self._mk(td, "book.m4b")
            self._mk(td, "song.flac")
            self._mk(td, "loose.mp3")
            self._mk(td, "album/01.flac"); self._mk(td, "album/02.mp3")   # mixed → music owns it
            self._mk(td, "mp3book/part1.mp3"); self._mk(td, "mp3book/part2.mp3")
            self._mk(td, "docs/readme.txt")
            C = IF._classify_entry
            self.assertEqual(C(os.path.join(td, "book.m4b")), "audiobook")
            self.assertEqual(C(os.path.join(td, "song.flac")), "music")
            self.assertEqual(C(os.path.join(td, "loose.mp3")), "audiobook")
            self.assertEqual(C(os.path.join(td, "album")), "music")
            self.assertEqual(C(os.path.join(td, "mp3book")), "audiobook")
            self.assertEqual(C(os.path.join(td, "docs")), "other")

    def test_entry_settled(self):
        from app import import_folder as IF
        with tempfile.TemporaryDirectory() as td:
            p = self._mk(td, "b/part1.mp3")
            d = os.path.join(td, "b")
            self.assertFalse(IF._entry_settled(d, time.time()))          # fresh
            old = time.time() - 600
            os.utime(p, (old, old)); os.utime(d, (old, old))
            self.assertTrue(IF._entry_settled(d, time.time()))           # old + quiet


if __name__ == "__main__":
    unittest.main()
