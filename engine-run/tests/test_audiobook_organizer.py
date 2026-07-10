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


class TestPartsAndAsin(unittest.TestCase):
    """Multi-part releases sort into ONE book; embedded ASINs skip the search entirely.
    Canonical example: 'Dark Age (Part 1 of 3) (Dramatized Adaptation) [B0FF5CWGK6]'."""

    EXAMPLE = "/x/Dark Age (Part 1 of 3) (Dramatized Adaptation) [B0FF5CWGK6].m4b"

    def test_asin_extracted(self):
        g = ab.infer_book_guess(self.EXAMPLE)
        self.assertEqual(g.get("asin"), "B0FF5CWGK6")

    def test_part_extracted_and_title_keeps_edition(self):
        g = ab.infer_book_guess(self.EXAMPLE)
        self.assertEqual(g.get("part"), 1)
        self.assertEqual(g.get("part_total"), 3)
        self.assertIn("Dark Age", g["title"])
        self.assertIn("Dramatized Adaptation", g["title"])   # edition qualifier is NOT an author
        self.assertNotIn("Part", g["title"])
        self.assertIsNone(g.get("author"))

    def test_strip_part_variants(self):
        self.assertEqual(ab.strip_part_info("Dark Age (Part 2 of 3)")[1:], (2, 3))
        self.assertEqual(ab.strip_part_info("Dark Age - Part 3")[1:], (3, None))
        self.assertEqual(ab.strip_part_info("Dark Age Pt. 2")[1:], (2, None))
        self.assertEqual(ab.strip_part_info("Dark Age (2 of 3)")[1:], (2, 3))
        self.assertEqual(ab.strip_part_info("Dark Age")[1:], (None, None))

    def test_parts_share_one_album_folder(self):
        meta1 = {"title": "Dark Age (Dramatized Adaptation)", "authors": ["Pierce Brown"],
                 "part": 1, "part_total": 3}
        meta2 = dict(meta1, part=2)
        d1 = ab.dest_for(meta1, "/lib")
        d2 = ab.dest_for(meta2, "/lib")
        self.assertEqual(os.path.dirname(d1), os.path.dirname(d2))   # SAME book folder
        self.assertTrue(d1.endswith("Dark Age (Dramatized Adaptation) - Part 01.m4b"))
        self.assertTrue(d2.endswith("Dark Age (Dramatized Adaptation) - Part 02.m4b"))

    def test_part_tags_group_and_order(self):
        meta = {"title": "Dark Age", "authors": ["Pierce Brown"], "narrators": [],
                "release_date": "", "summary": "", "image": "", "genres": [],
                "series": "", "series_position": "", "part": 2, "part_total": 3}
        t = ab.build_mp4_tags(meta)
        self.assertEqual(t["\xa9alb"], "Dark Age")            # shared album = one Plex book
        self.assertEqual(t["\xa9nam"], "Dark Age - Part 2")
        self.assertEqual(t["trkn"], [(2, 3)])

    def test_apply_part_info_from_audnexus_title(self):
        # the part product's OWN Audnexus title carries the marker — must not become
        # a separate per-part book
        meta = {"title": "Dark Age (Part 1 of 3) (Dramatized Adaptation)",
                "authors": ["Pierce Brown"]}
        ab._apply_part_info(meta, {"title": "whatever"})
        self.assertEqual(meta["title"], "Dark Age (Dramatized Adaptation)")
        self.assertEqual(meta["part"], 1)
        self.assertEqual(meta["part_total"], 3)


class TestPartAwareMatching(unittest.TestCase):
    DARK_AGE_CANDS = [
        {"asin": "P3", "title": "Dark Age (3 of 3) [Dramatized Adaptation]", "authors": ["Pierce Brown"]},
        {"asin": "P1", "title": "Dark Age (1 of 3) [Dramatized Adaptation]", "authors": ["Pierce Brown"]},
        {"asin": "P2", "title": "Dark Age (2 of 3) [Dramatized Adaptation]", "authors": ["Pierce Brown"]},
    ]

    def test_part_file_matches_its_own_part_product(self):
        # part 3 lists FIRST in real search results — a part-1 file must still pick part 1
        g = {"title": "Dark Age (Dramatized Adaptation)", "author": None, "part": 1, "part_total": 3}
        best, score = ab.pick_candidate(g, self.DARK_AGE_CANDS)
        self.assertIsNotNone(best)
        self.assertEqual(best["asin"], "P1")

    def test_partless_guess_still_matches(self):
        g = {"title": "Dark Age (Dramatized Adaptation)", "author": None}
        best, _ = ab.pick_candidate(g, self.DARK_AGE_CANDS)
        self.assertIsNotNone(best)

    def test_subtitle_split_from_underscore(self):
        g = ab.infer_book_guess(
            "/x/Dark Age (Part 1 of 3) (Dramatized Adaptation)_ Red Rising, Book 5 [B0FF5CWGK6].m4b")
        self.assertNotIn("Red Rising", g["title"])     # subtitle dropped from the search title
        self.assertIn("Dramatized Adaptation", g["title"])
        self.assertEqual(g.get("part"), 1)
        self.assertEqual(g.get("asin"), "B0FF5CWGK6")

    def test_author_role_filtered(self):
        meta = {"title": "The Art of War",
                "authors": ["translation by John Minford", "Sun Tzu"]}
        self.assertEqual(ab._pick_author(meta), "Sun Tzu")
        self.assertEqual(ab._pick_author({"authors": ["Pierce Brown"]}), "Pierce Brown")
        # all-role list still returns something rather than crashing
        self.assertEqual(ab._pick_author({"authors": ["translation by X"]}), "translation by X")

    def test_search_ladder_strips_parens_on_retry(self):
        calls = []
        def fake_search(title, author=None, session=None):
            calls.append(title)
            return [] if "(" in title else [{"asin": "A", "title": title, "authors": []}]
        with mock.patch.object(ab, "audible_search", fake_search):
            cands = ab._search_ladder({"title": "Dark Age (Dramatized Adaptation)", "author": None})
        self.assertEqual(len(cands), 1)
        self.assertEqual(calls[0], "Dark Age (Dramatized Adaptation)")
        self.assertEqual(calls[1], "Dark Age")


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


class TestPlanPlexReconcile(unittest.TestCase):
    """Models the real 2026-07-10 split: Dark Age parts landed as TWO albums (parts 1+3 under the
    agent-matched album, part 2 under a duplicate local artist), both titled with the part-3
    agent product, parts 1+2 missing track numbers."""

    DIR = "/audiobooks/Pierce Brown/Dark Age [Dramatized Adaptation]"

    def _track(self, key, index, part):
        return {"key": key, "index": index,
                "file": f"{self.DIR}/Dark Age [Dramatized Adaptation] - Part {part:02d}.m4b"}

    def test_split_album_merged_retitled_reindexed(self):
        albums = [
            {"key": 142121, "title": "Dark Age (3 of 3) [Dramatized Adaptation]: Red Rising 5",
             "dir": self.DIR, "agent_matched": True,
             "tracks": [self._track(142125, None, 1), self._track(142122, 3, 3)]},
            {"key": 142126, "title": "Dark Age (3 of 3) [Dramatized Adaptation]: Red Rising 5",
             "dir": self.DIR, "agent_matched": False,
             "tracks": [self._track(142127, None, 2)]},
        ]
        plan = ab.plan_plex_reconcile(albums)
        self.assertEqual(plan["merges"], [(142121, [142126])])
        self.assertEqual(plan["retitles"], [(142121, "Dark Age [Dramatized Adaptation]")])
        self.assertEqual(sorted(plan["reindexes"]), [(142125, 1), (142127, 2)])

    def test_clean_multipart_album_is_noop(self):
        albums = [{"key": 1, "title": "Dark Age [Dramatized Adaptation]", "dir": self.DIR,
                   "agent_matched": True,
                   "tracks": [self._track(11, 1, 1), self._track(12, 2, 2), self._track(13, 3, 3)]}]
        plan = ab.plan_plex_reconcile(albums)
        self.assertEqual(plan, {"merges": [], "retitles": [], "reindexes": []})

    def test_single_file_book_untouched(self):
        # single books keep their (often richer) agent title — no part marker, no intervention
        albums = [{"key": 2, "title": "The Art of War: The Definitive Edition",
                   "dir": "/audiobooks/Sun Tzu/The Art of War", "agent_matched": True,
                   "tracks": [{"key": 21, "index": 1,
                               "file": "/audiobooks/Sun Tzu/The Art of War/The Art of War.m4b"}]}]
        plan = ab.plan_plex_reconcile(albums)
        self.assertEqual(plan, {"merges": [], "retitles": [], "reindexes": []})

    def test_unsplit_multipart_with_part_title_still_retitled(self):
        albums = [{"key": 3, "title": "Dark Age (1 of 3) [Dramatized Adaptation]", "dir": self.DIR,
                   "agent_matched": True, "tracks": [self._track(31, 1, 1)]}]
        plan = ab.plan_plex_reconcile(albums)
        self.assertEqual(plan["merges"], [])
        self.assertEqual(plan["retitles"], [(3, "Dark Age [Dramatized Adaptation]")])

    def test_merge_prefers_agent_matched_album(self):
        albums = [
            {"key": 5, "title": "X", "dir": self.DIR, "agent_matched": False,
             "tracks": [self._track(51, 1, 1), self._track(52, 2, 2)]},
            {"key": 4, "title": "X", "dir": self.DIR, "agent_matched": True,
             "tracks": [self._track(41, 3, 3)]},
        ]
        plan = ab.plan_plex_reconcile(albums)
        self.assertEqual(plan["merges"], [(4, [5])])


if __name__ == "__main__":
    unittest.main()
