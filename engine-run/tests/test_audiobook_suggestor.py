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

    def test_music_track_with_title_words_rejected(self):
        # THE live incident: 'Master Alvin' (Orson Scott Card, 1217 min) must NOT match a
        # Prodigy 'Alvin Risk Remix.m4a' just because 'master'+'alvin' are in the path
        mb = 1048576
        responses = [self._resp("CherrySeed", [
            (r"music\Official Releases (Albums - Master)\The Prodigy - 1997 - The Fat Of The Land\12 - Firestarter (Alvin Risk Remix).m4a", 29 * mb)])]
        self.assertIsNone(
            sg.pick_audiobook_dir(responses, "Master Alvin", "Orson Scott Card", 1217))

    def test_lone_m4a_is_music_not_book(self):
        mb = 1048576
        responses = [self._resp("p", [(r"x\Some Book\Some Book.m4a", 300 * mb)])]
        # even sized right and with author, a bare .m4a isn't an audiobook (those are .m4b)
        self.assertIsNone(sg.pick_audiobook_dir(responses, "Some Book", "", 600))

    def test_impossible_size_for_runtime_rejected(self):
        mb = 1048576
        # a 20-hour (1200 min) book cannot be 40 MB
        responses = [self._resp("p", [(r"b\Long Epic\long.m4b", 40 * mb)])]
        self.assertIsNone(sg.pick_audiobook_dir(responses, "Long Epic", "Someone", 1200))

    def test_title_only_no_author_no_runtime_rejected(self):
        mb = 1048576
        # title words present but nothing corroborates it's the right book -> don't accept
        responses = [self._resp("p", [(r"stuff\Common Words\Common Words.m4b", 200 * mb)])]
        self.assertIsNone(sg.pick_audiobook_dir(responses, "Common Words", "", None))

    def test_author_in_path_accepts_without_runtime(self):
        mb = 1048576
        responses = [self._resp("p", [(r"Books\Andy Weir\The Martian\The Martian.m4b", 300 * mb)])]
        best = sg.pick_audiobook_dir(responses, "The Martian", "Andy Weir", None)
        self.assertIsNotNone(best)


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
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_slskd_search", return_value=None):
            out = sg.acquire_pass(self.imports)
        self.assertEqual(out["retried"], 1)
        w = sg.load_wants()[0]
        self.assertEqual(w["attempts"], 1)
        self.assertGreater(w["next_try_at"], time.time())

    def test_unconfigured_skips_without_burning_retries(self):
        sg.add_want({"asin": "A1", "title": "Obscure Book"})
        with mock.patch("app.slskd_client.configured", return_value=False):
            out = sg.acquire_pass(self.imports)
        self.assertIn("skipped", out)
        self.assertEqual(sg.load_wants()[0]["attempts"], 0)   # NOT burned

    def test_hit_enqueues_all_and_marks_downloading(self):
        sg.add_want({"asin": "A1", "title": "Found Book", "author": "Someone"})
        pick = {"peer": "peer1", "dir": "d", "total_mb": 300,
                "files": [{"filename": "d\\Found Book.m4b", "size": 100}]}
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_slskd_search", return_value=pick), \
             mock.patch("app.slskd_client.enqueue_download", return_value=True), \
             mock.patch("app.slskd_client.get_transfers_for_user", return_value=[]):
            out = sg.acquire_pass(self.imports)
        self.assertEqual(out["started"], 1)
        w = sg.load_wants()[0]
        self.assertEqual((w["status"], w["peer"]), ("downloading", "peer1"))

    def test_partial_enqueue_cancels_and_retries(self):
        sg.add_want({"asin": "A1", "title": "Book", "author": "X"})
        pick = {"peer": "p", "dir": "d", "total_mb": 100,
                "files": [{"filename": "d\\a.mp3", "size": 1}, {"filename": "d\\b.mp3", "size": 1}]}
        cancels = []
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_slskd_search", return_value=pick), \
             mock.patch("app.slskd_client.enqueue_download",
                        side_effect=[True, False]), \
             mock.patch("app.slskd_client.cancel_downloads",
                        side_effect=lambda u, f: cancels.append((u, list(f)))), \
             mock.patch("app.slskd_client.get_transfers_for_user", return_value=[]):
            out = sg.acquire_pass(self.imports)
        self.assertEqual(out["retried"], 1)
        self.assertTrue(cancels)                         # the one accepted file was cancelled

    def test_one_search_per_pass(self):
        sg.add_want({"asin": "A1", "title": "Book One"})
        sg.add_want({"asin": "A2", "title": "Book Two"})
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_slskd_search", return_value=None) as m:
            sg.acquire_pass(self.imports)
        self.assertEqual(m.call_count, 1)


class TestCollectDelivered(unittest.TestCase):
    def test_moves_all_into_one_folder(self):
        with tempfile.TemporaryDirectory() as d:
            comp = os.path.join(d, "complete", "Found Book")   # parent dir matches want's suffix
            os.makedirs(comp)
            open(os.path.join(comp, "01.mp3"), "w").write("x")
            open(os.path.join(comp, "02.mp3"), "w").write("x")
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            want = {"author": "Andy Weir", "title": "Found Book",
                    "files": ["share\\Found Book\\01.mp3", "share\\Found Book\\02.mp3"]}
            moved = sg._collect_delivered(want, imports, roots=[os.path.join(d, "complete")])
            self.assertEqual(sorted(moved), ["01.mp3", "02.mp3"])
            self.assertEqual(len(os.listdir(os.path.join(imports, "Andy Weir - Found Book"))), 2)

    def test_partial_delivery_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            comp = os.path.join(d, "complete", "Found Book"); os.makedirs(comp)
            open(os.path.join(comp, "01.mp3"), "w").write("x")   # only 1 of 2 landed
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            want = {"title": "Found Book",
                    "files": ["s\\Found Book\\01.mp3", "s\\Found Book\\02.mp3"]}
            self.assertEqual(sg._collect_delivered(want, imports,
                                                   roots=[os.path.join(d, "complete")]), [])
            self.assertEqual(os.listdir(imports), [])            # nothing moved yet

    def test_generic_name_in_other_book_not_stolen(self):
        # a different book's '01.mp3' (different parent dir) must NOT be collected
        with tempfile.TemporaryDirectory() as d:
            other = os.path.join(d, "complete", "Other Book"); os.makedirs(other)
            open(os.path.join(other, "01.mp3"), "w").write("OTHER")
            imports = os.path.join(d, "imports"); os.makedirs(imports)
            want = {"title": "My Book", "files": ["s\\My Book\\01.mp3"]}
            self.assertEqual(sg._collect_delivered(want, imports,
                                                   roots=[os.path.join(d, "complete")]), [])
            self.assertTrue(os.path.isfile(os.path.join(other, "01.mp3")))   # untouched




class TestSoulseekAvailability(unittest.TestCase):
    def _dir(self, title):
        mb = 1048576
        return [{"username": "p", "queueLength": 0,
                 "files": [{"filename": f"share\\{title}\\{title}.m4b", "size": 400 * mb}]}]

    def test_on_soulseek_true_when_pick_found(self):
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch("app.slskd_client.search", return_value="sid"), \
             mock.patch("app.slskd_client.get_search_results",
                        return_value=self._dir("Project Hail Mary")):
            self.assertTrue(sg.on_soulseek({"title": "Project Hail Mary", "runtime_min": 970}))

    def test_on_soulseek_false_when_nothing(self):
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch("app.slskd_client.search", return_value="sid"), \
             mock.patch("app.slskd_client.get_search_results", return_value=[]):
            self.assertFalse(sg.on_soulseek({"title": "Nonexistent Book"}))

    def test_unconfigured_is_false(self):
        with mock.patch("app.slskd_client.configured", return_value=False):
            self.assertFalse(sg.on_soulseek({"title": "X"}))

    def test_filter_keeps_only_available(self):
        items = [{"asin": "A", "title": "Has It"}, {"asin": "B", "title": "Nope"},
                 {"asin": "C", "title": "Also Has"}]
        avail = {"Has It", "Also Has"}
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_PROBE_GAP_S", 0), \
             mock.patch.object(sg, "on_soulseek", side_effect=lambda it, *a, **k: it["title"] in avail):
            out = sg.filter_on_soulseek(items, keep=10)
        self.assertEqual({i["asin"] for i in out}, {"A", "C"})

    def test_filter_stops_at_keep(self):
        items = [{"asin": str(i), "title": f"B{i}"} for i in range(10)]
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_PROBE_GAP_S", 0), \
             mock.patch.object(sg, "on_soulseek", return_value=True):
            out = sg.filter_on_soulseek(items, keep=3)
        self.assertEqual(len(out), 3)

    def test_filter_respects_max_probes(self):
        # never probe more than max_probes even if fewer than keep are confirmed
        items = [{"asin": str(i), "title": f"B{i}"} for i in range(30)]
        calls = []
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "_PROBE_GAP_S", 0), \
             mock.patch.object(sg, "on_soulseek",
                               side_effect=lambda it, *a, **k: calls.append(it) or False):
            sg.filter_on_soulseek(items, keep=15, max_probes=8)
        self.assertEqual(len(calls), 8)


class TestSearchCatalog(unittest.TestCase):
    def test_empty_query_returns_empty(self):
        self.assertEqual(sg.search_catalog(""), [])

    def test_single_char_is_allowed(self):
        # 'type f -> f things show up' — 1-char search must work
        products = {"products": [
            {"asin": "F1", "title": "Fahrenheit 451", "authors": [{"name": "Ray Bradbury"}],
             "runtime_length_min": 300, "language": "english"},
            {"asin": "X1", "title": "A Book", "authors": [{"name": "Someone"}],
             "runtime_length_min": 400, "language": "english"}]}
        with mock.patch.object(sg, "_audible_get", return_value=products):
            out = sg.search_catalog("f")
        self.assertIn("F1", [c["asin"] for c in out])

    def test_search_is_fast_no_soulseek_probe(self):
        # search must NOT touch slskd (availability is a separate call)
        products = {"products": [
            {"asin": "S1", "title": "Sci Fi One", "authors": [{"name": "A"}],
             "runtime_length_min": 600, "language": "english"},
            {"asin": "S2", "title": "Sci Fi Two", "authors": [{"name": "A"}],
             "runtime_length_min": 600, "language": "english"}]}
        with mock.patch.object(sg, "_audible_get", return_value=products), \
             mock.patch.object(sg, "on_soulseek", side_effect=AssertionError("must not probe")):
            out = sg.search_catalog("sci fi")
        self.assertEqual({c["asin"] for c in out}, {"S1", "S2"})
        self.assertEqual(out[0]["reason"], "search result")

    def test_startswith_query_floats_up(self):
        products = {"products": [
            {"asin": "MID", "title": "The Fault in Our Stars", "authors": [{"name": "A"}],
             "runtime_length_min": 400, "language": "english"},
            {"asin": "PREFIX", "title": "Fault Lines", "authors": [{"name": "B"}],
             "runtime_length_min": 400, "language": "english"}]}
        with mock.patch.object(sg, "_audible_get", return_value=products):
            out = sg.search_catalog("fault")
        self.assertEqual(out[0]["asin"], "PREFIX")   # title starting with 'fault' ranks first

    def test_search_shows_owned_but_hides_wanted(self):
        products = {"products": [
            {"asin": "OWNED", "title": "Owned Book", "authors": [{"name": "A"}],
             "runtime_length_min": 600, "language": "english"},
            {"asin": "WANTED", "title": "Wanted Book", "authors": [{"name": "A"}],
             "runtime_length_min": 600, "language": "english"}]}
        with mock.patch.object(sg, "_audible_get", return_value=products), \
             mock.patch.object(sg, "load_wants", return_value=[{"asin": "WANTED"}]):
            out = sg.search_catalog("book")
        self.assertEqual([c["asin"] for c in out], ["OWNED"])


class TestAvailability(unittest.TestCase):
    ITEMS = [{"asin": "A", "title": "Has It"}, {"asin": "B", "title": "Nope"},
             {"asin": "C", "title": "Also Has"}]

    def test_batch_availability_map(self):
        avail = {"Has It", "Also Has"}
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "on_soulseek",
                               side_effect=lambda it, *a, **k: it["title"] in avail):
            out = sg.availability(self.ITEMS)
        self.assertEqual(out, {"A": True, "B": False, "C": True})

    def test_unconfigured_returns_empty(self):
        with mock.patch("app.slskd_client.configured", return_value=False):
            self.assertEqual(sg.availability(self.ITEMS), {})

    def test_items_without_asin_skipped(self):
        with mock.patch("app.slskd_client.configured", return_value=True), \
             mock.patch.object(sg, "on_soulseek", return_value=True):
            out = sg.availability([{"title": "no asin"}, {"asin": "Z", "title": "z"}])
        self.assertEqual(out, {"Z": True})


if __name__ == "__main__":
    unittest.main()
