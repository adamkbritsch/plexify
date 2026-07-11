"""Tests for the audiobook suggestor + wanted-list acquisition state machine."""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from app import audiobook_suggestor as sg


class TestFilterCandidates(unittest.TestCase):
    OWNED = [{"asin": "OWN1", "title": "Red Rising", "author": "Pierce Brown"},
             {"asin": "", "title": "The Martian", "author": "Andy Weir"}]

    def test_owned_asin_and_title_dupes_dropped(self):
        cands = [
            {"asin": "OWN1", "title": "Red Rising", "author": "Pierce Brown", "runtime_min": 900},
            {"asin": "X1", "title": "The Martian (Unabridged)", "author": "Andy Weir",
             "runtime_min": 650},
            {"asin": "X2", "title": "Golden Son", "author": "Pierce Brown", "runtime_min": 1100},
        ]
        out = sg.filter_candidates(cands, self.OWNED, [], [])
        self.assertEqual([c["asin"] for c in out], ["X2"])

    def test_wants_dismissed_and_shorts_dropped(self):
        cands = [
            {"asin": "W1", "title": "Wanted Book", "author": "A", "runtime_min": 500},
            {"asin": "D1", "title": "Dismissed Book", "author": "A", "runtime_min": 500},
            {"asin": "S1", "title": "A Sample", "author": "A", "runtime_min": 10},
            {"asin": "K1", "title": "Keeper", "author": "A", "runtime_min": 500},
        ]
        out = sg.filter_candidates(cands, [], [{"asin": "W1"}], ["D1"])
        self.assertEqual([c["asin"] for c in out], ["K1"])

    def test_same_title_different_author_kept(self):
        cands = [{"asin": "X", "title": "Red Rising", "author": "Someone Else",
                  "runtime_min": 400}]
        out = sg.filter_candidates(cands, self.OWNED, [], [])
        self.assertEqual(len(out), 1)


class TestBackoff(unittest.TestCase):
    def test_schedule_then_gave_up(self):
        w = {"status": "wanted", "attempts": 0, "next_try_at": 0}
        hours = []
        for _ in range(len(sg._BACKOFF_HOURS)):
            sg.apply_retry(w, "nope")
            self.assertEqual(w["status"], "wanted")
            hours.append(round((w["next_try_at"] - time.time()) / 3600))
        self.assertEqual(hours, [2, 6, 12, 24, 24, 24, 24])
        sg.apply_retry(w, "nope")
        self.assertEqual(w["status"], "gave_up")

    def test_total_window_is_multiple_days(self):
        self.assertGreaterEqual(sum(sg._BACKOFF_HOURS), 96)   # "over the next couple days"


class TestPickAudiobookDir(unittest.TestCase):
    def _resp(self, peer, files, queue=0):
        return {"username": peer, "queueLength": queue,
                "files": [{"filename": f, "size": s} for f, s in files]}

    def test_m4b_wins_over_mp3_pile(self):
        mb = 1048576
        responses = [
            self._resp("p1", [(r"share\books\Project Hail Mary\Project Hail Mary.m4b", 500 * mb)]),
            self._resp("p2", [(rf"stuff\Project Hail Mary\{i:02d}.mp3", 20 * mb)
                              for i in range(1, 17)]),
        ]
        best = sg.pick_audiobook_dir(responses, "Project Hail Mary", "Andy Weir", 970)
        self.assertEqual(best["peer"], "p1")

    def test_title_tokens_required(self):
        mb = 1048576
        responses = [self._resp("p", [(r"music\Totally Different Album\01.mp3", 60 * mb)])]
        self.assertIsNone(sg.pick_audiobook_dir(responses, "Project Hail Mary", "", None))

    def test_flac_dir_is_music_not_book(self):
        mb = 1048576
        responses = [self._resp("p", [
            (r"music\Project Hail Mary OST\01.flac", 40 * mb),
            (r"music\Project Hail Mary OST\02.flac", 40 * mb),
            (r"music\Project Hail Mary OST\cover.mp3", 1 * mb),
        ])]
        self.assertIsNone(sg.pick_audiobook_dir(responses, "Project Hail Mary", "", None))

    def test_runtime_size_band(self):
        mb = 1048576
        responses = [
            self._resp("tiny", [(r"b\Project Hail Mary\phm.m4b", 30 * mb)]),
            self._resp("right", [(r"b\Project Hail Mary\phm.m4b", 450 * mb)]),
        ]
        best = sg.pick_audiobook_dir(responses, "Project Hail Mary", "", 970)
        self.assertEqual(best["peer"], "right")

    def test_junk_words_skipped(self):
        mb = 1048576
        responses = [self._resp("p", [(r"b\Project Hail Mary SAMPLE\phm.m4b", 100 * mb)])]
        self.assertIsNone(sg.pick_audiobook_dir(responses, "Project Hail Mary", "", None))


class TestWantsStore(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = sg.DATA_DIR
        sg.DATA_DIR = self._dir

    def tearDown(self):
        sg.DATA_DIR = self._orig

    def test_add_dedupe_remove(self):
        self.assertTrue(sg.add_want({"asin": "A1", "title": "T", "author": "Au"})["ok"])
        self.assertTrue(sg.add_want({"asin": "A1", "title": "T"})["already"])
        self.assertEqual(len(sg.load_wants()), 1)
        self.assertEqual(sg.remove_want("A1")["removed"], 1)
        self.assertEqual(sg.load_wants(), [])

    def test_wanted_status_shape(self):
        sg.add_want({"asin": "A1", "title": "T", "author": "Au", "runtime_min": 700,
                     "reason": "more by Au"})
        st = sg.wanted_status()
        self.assertEqual(st[0]["status"], "wanted")
        self.assertEqual(st[0]["next_try_in_s"], 0)   # first try is immediate


class TestAcquirePassSearch(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._orig = sg.DATA_DIR
        sg.DATA_DIR = self._dir
        self.imports = os.path.join(self._dir, "imports")
        os.makedirs(self.imports)

    def tearDown(self):
        sg.DATA_DIR = self._orig

    def test_no_result_applies_backoff(self):
        sg.add_want({"asin": "A1", "title": "Obscure Book", "author": "Nobody"})
        with mock.patch.object(sg, "_slskd_search", return_value=None):
            out = sg.acquire_pass(self.imports)
        self.assertEqual(out["retried"], 1)
        w = sg.load_wants()[0]
        self.assertEqual(w["attempts"], 1)
        self.assertGreater(w["next_try_at"], time.time())

    def test_hit_enqueues_and_marks_downloading(self):
        sg.add_want({"asin": "A1", "title": "Found Book", "author": "Someone"})
        pick = {"peer": "peer1", "dir": "d", "total_mb": 300,
                "files": [{"filename": "d\\Found Book.m4b", "size": 100}]}
        fake_slskd = mock.Mock()
        fake_slskd.enqueue_download.return_value = True
        with mock.patch.object(sg, "_slskd_search", return_value=pick), \
             mock.patch.dict("sys.modules", {}), \
             mock.patch("app.slskd_client.enqueue_download", return_value=True), \
             mock.patch("app.slskd_client.get_transfers_for_user", return_value=[]):
            out = sg.acquire_pass(self.imports)
        self.assertEqual(out["started"], 1)
        w = sg.load_wants()[0]
        self.assertEqual(w["status"], "downloading")
        self.assertEqual(w["peer"], "peer1")

    def test_one_search_per_pass(self):
        sg.add_want({"asin": "A1", "title": "Book One"})
        sg.add_want({"asin": "A2", "title": "Book Two"})
        with mock.patch.object(sg, "_slskd_search", return_value=None) as m:
            sg.acquire_pass(self.imports)
        self.assertEqual(m.call_count, 1)


class TestCollectDelivered(unittest.TestCase):
    def test_moves_into_one_import_folder(self):
        with tempfile.TemporaryDirectory() as d:
            comp = os.path.join(d, "downloads_music", "complete", "peerdir")
            os.makedirs(comp)
            open(os.path.join(comp, "01.mp3"), "w").write("x")
            open(os.path.join(comp, "02.mp3"), "w").write("x")
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            want = {"author": "Andy Weir", "title": "Found Book",
                    "files": ["share\\x\\01.mp3", "share\\x\\02.mp3"]}
            moved = sg._collect_delivered(want, imports,
                                          roots=[os.path.join(d, "downloads_music", "complete")])
            self.assertEqual(sorted(moved), ["01.mp3", "02.mp3"])
            book_dir = os.path.join(imports, "Andy Weir - Found Book")
            self.assertTrue(os.path.isdir(book_dir))
            self.assertEqual(len(os.listdir(book_dir)), 2)


if __name__ == "__main__":
    unittest.main()
