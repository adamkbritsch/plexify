"""Tests for the unified import router + the name-inference upgrade.

The corpus mirrors the REAL plexify-imports drop (2026-07): reading-order/series-code
prefixes ('01 FW1.0 Earth Unaware'), 'Title by Author Book N' series rips, trailing
narrator segments, unicode colon stand-ins, {…} qualifiers with 'read by' suffixes,
CamelCase concatenations, an m4b collection folder, nested and multi-disc mp3 folders,
and FLAC that belongs to the music pipeline.
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from app import audiobook_organizer as ab


def _mk(root, rel, data=b"x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def _age(root, secs=600):
    """utime every file AND dir under root to the past — _import_settled requires the
    newest mtime to be >= _SETTLE_SECS old."""
    old = time.time() - secs
    for dp, _dns, fns in os.walk(root, topdown=False):
        for fn in fns:
            os.utime(os.path.join(dp, fn), (old, old))
        os.utime(dp, (old, old))


class TestInferRealDropNames(unittest.TestCase):
    """infer_book_guess({} tags) on the exact basenames observed in the real drop."""

    def _g(self, name):
        return ab.infer_book_guess("/imports/" + name, {})

    def test_order_code_prefix_earth_unaware(self):
        g = self._g("01 FW1.0 Earth Unaware.m4b")
        self.assertEqual(g["title"], "Earth Unaware")
        self.assertIsNone(g["author"])

    def test_order_code_prefix_with_decimals(self):
        g = self._g("24.1 ES6.1 The Last Shadow.m4b")
        self.assertEqual(g["title"], "The Last Shadow")
        self.assertIsNone(g["author"])

    def test_book_n_series_prefix_and_year(self):
        g = self._g("Book 4 - Harry Potter and the Goblet of Fire (2000)")
        self.assertEqual(g["title"], "Harry Potter and the Goblet of Fire")
        self.assertIsNone(g["author"])

    def test_title_by_author_book_n(self):
        g = self._g("Dark Age by Pierce Brown Book 5.m4b")
        self.assertEqual(g["title"], "Dark Age")
        self.assertEqual(g["author"], "Pierce Brown")

    def test_title_by_author_book_n_second_example(self):
        g = self._g("Golden Son by Pierce Brown Book 2.m4b")
        self.assertEqual(g["title"], "Golden Son")
        self.assertEqual(g["author"], "Pierce Brown")

    def test_three_segment_narrator_dropped(self):
        g = self._g("Alexandre Dumas - The Count of Monte Cristo (2008) - John Lee.m4b")
        self.assertEqual(g["title"], "The Count of Monte Cristo")
        self.assertEqual(g["author"], "Alexandre Dumas")

    def test_braces_and_read_by_suffix_iliad(self):
        g = self._g("The Iliad {Robert Fitzgerald Transl} read by Dan Stevens")
        self.assertEqual(g["title"], "The Iliad")
        self.assertIsNone(g["author"])

    def test_braces_and_read_by_suffix_odyssey(self):
        g = self._g("The Odyssey {Robert Fitzgerald Transl} read by Dan Stevens")
        self.assertEqual(g["title"], "The Odyssey")
        self.assertIsNone(g["author"])

    def test_unicode_colon_subtitle_split(self):
        # U+A789 colon stand-in + stray ']' — subtitle splits off the search title
        g = self._g("Sunrise on the Reaping꞉ A Hunger Games Novel].m4b")
        self.assertEqual(g["title"], "Sunrise on the Reaping")
        self.assertIsNone(g["author"])
        self.assertEqual(g.get("subtitle"), "A Hunger Games Novel")

    def test_camelcase_concatenation(self):
        g = self._g("AManCalledOveUnabridged.mp3")
        self.assertEqual(g["title"], "A Man Called Ove")
        self.assertIsNone(g["author"])

    def test_title_dash_author_orientation_with_double_space(self):
        g = self._g("The Wolf of Wall Street  - Jordan Belfort")
        self.assertEqual(g["title"], "The Wolf of Wall Street")
        self.assertEqual(g["author"], "Jordan Belfort")

    # ── regression must-holds ──────────────────────────────────────────────────

    def test_regression_asin_and_part_still_extracted(self):
        g = self._g("Dark Age (Part 1 of 3) (Dramatized Adaptation)_ Red Rising, Book 5 "
                    "[B0FF5CWGK6].m4b")
        self.assertEqual(g.get("asin"), "B0FF5CWGK6")
        self.assertEqual(g.get("part"), 1)
        self.assertEqual(g.get("part_total"), 3)
        self.assertNotIn("Red Rising", g["title"])
        self.assertIn("Dark Age", g["title"])

    def test_regression_death_by_chocolate_keeps_no_author(self):
        # 'by' inside a title must never mint a one-word fake author
        g = self._g("Death by Chocolate.m4b")
        self.assertEqual(g["title"], "Death by Chocolate")
        self.assertIsNone(g["author"])

    def test_tag_branch_gets_same_normalization(self):
        # auto-m4b copies the rip's album tag verbatim — order-code prefixes and
        # friends must be cleaned there too, not only in filenames
        g = ab.infer_book_guess("/x/whatever_rip_001.m4b",
                                {"album": "01 FW1.0 Earth Unaware",
                                 "albumartist": "Orson Scott Card"})
        self.assertEqual(g["title"], "Earth Unaware")
        self.assertEqual(g["author"], "Orson Scott Card")


class TestRouteImportsReplica(unittest.TestCase):
    """route_imports on a synthetic replica of the real plexify-imports tree.

    Mechanics under test: two-pass size memo (first call routes NOTHING), 120s mtime
    settle, and every routing shape — bare files, 1-m4b folders, collections, nested
    and multi-disc mp3 folders, mixed folders, FLAC hand-off."""

    BARE_M4BS = [
        "01 FW1.0 Earth Unaware",
        "Red Rising by Pierce Brown Book 1",
        "Dark Age by Pierce Brown Book 5",
        "Sunrise on the Reaping꞉ A Hunger Games Novel]",
    ]
    COLLECTION = "Andy Weir - Audiobook Collection"
    COLLECTION_BOOKS = ["Project Hail Mary", "The Martian", "Artemis", "The Egg",
                        "Randomize", "Cheshire Crossing", "Annie's Day"]
    MP3_FOLDERS = {
        "Book 1 - The Hunger Games": ["01.mp3", "02.mp3", "03.mp3", "cover.jpg"],
        "Book 4 - Harry Potter and the Goblet of Fire (2000)": ["01.mp3", "02.mp3", "03.mp3"],
        "The Wolf of Wall Street  - Jordan Belfort": ["01.mp3", "02.mp3"],   # double space
    }
    N_AUDIOBOOK_ENTRIES = 15     # top-level audiobook-shaped entries (incl. the fresh one)
    N_MUSIC_ENTRIES = 2

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="ab-route-")
        self.addCleanup(shutil.rmtree, self.td, True)
        self.imp = os.path.join(self.td, "plexify-imports")
        self.temp = os.path.join(self.td, "ab-temp")
        self.intake = os.path.join(self.temp, "recentlyadded")
        self.untagged = os.path.join(self.temp, "untagged")
        for d in (self.imp, self.intake, self.untagged):
            os.makedirs(d)

        for stem in self.BARE_M4BS:
            _mk(self.imp, stem + ".m4b")
        _mk(self.imp, "AManCalledOveUnabridged.mp3")
        for folder, files in self.MP3_FOLDERS.items():
            for fn in files:
                _mk(self.imp, os.path.join(folder, fn))
        for b in self.COLLECTION_BOOKS:
            _mk(self.imp, os.path.join(self.COLLECTION, b + ".m4b"))
        _mk(self.imp, os.path.join(self.COLLECTION, "cover.jpg"))     # sidecar-only leftover
        for i in (1, 2, 3):
            _mk(self.imp, os.path.join("Lord Of The Flies", "_", "%02d.mp3" % i))
        _mk(self.imp, "loose song.flac")
        _mk(self.imp, os.path.join("Some Flac Album", "a.flac"))
        _mk(self.imp, os.path.join("Some Flac Album", "b.flac"))
        _mk(self.imp, os.path.join("Some Great Book (2019)", "audiobook.m4b"))
        _mk(self.imp, os.path.join("hp7", "Harry Potter and the Deathly Hallows.m4b"))
        _mk(self.imp, os.path.join("Mixed Book", "Mixed Book.m4b"))
        _mk(self.imp, os.path.join("Mixed Book", "p1.mp3"))
        _mk(self.imp, os.path.join("Mixed Book", "p2.mp3"))
        for cd in ("CD1", "CD2"):
            for i in (1, 2):
                _mk(self.imp, os.path.join("Multi Disc Book", cd, "%02d.mp3" % i))

        _age(self.imp)                       # everything old enough — files AND dirs
        _mk(self.imp, "Fresh Book.m4b")      # created AFTER aging: still-being-copied entry

        patcher = mock.patch.object(ab, "DATA_DIR", os.path.join(self.td, "data"))
        patcher.start()
        self.addCleanup(patcher.stop)
        ab._route_size_memo.clear()
        self.addCleanup(ab._route_size_memo.clear)

        self.waiting_before = ab.imports_waiting(self.imp)
        self.first = ab.route_imports(self.imp, self.temp)
        self.untagged_after_first = os.listdir(self.untagged)
        self.intake_after_first = os.listdir(self.intake)
        self.second = ab.route_imports(self.imp, self.temp)
        self.waiting_after = ab.imports_waiting(self.imp)

    # ── two-pass settle mechanics ─────────────────────────────────────────────

    def test_first_pass_routes_nothing(self):
        self.assertEqual(self.first["to_untagged"], 0)
        self.assertEqual(self.first["to_convert"], 0)
        self.assertEqual(self.first["skipped_unsettled"], self.N_AUDIOBOOK_ENTRIES)
        self.assertEqual(self.first["left_for_music"], self.N_MUSIC_ENTRIES)
        self.assertEqual(self.untagged_after_first, [])
        self.assertEqual(self.intake_after_first, [])

    def test_second_pass_counters(self):
        self.assertEqual(self.second["to_untagged"], 14)   # 4 bare + 7 collection + 3 foldered
        self.assertEqual(self.second["to_convert"], 7)
        self.assertEqual(self.second["skipped_unsettled"], 1)   # the fresh entry only
        self.assertEqual(self.second["left_for_music"], self.N_MUSIC_ENTRIES)
        self.assertEqual(self.second["errors"], 0)

    def test_fresh_entry_stays_put(self):
        self.assertTrue(os.path.isfile(os.path.join(self.imp, "Fresh Book.m4b")))

    # ── shapes ────────────────────────────────────────────────────────────────

    def test_bare_m4bs_get_folder_per_book(self):
        for stem in self.BARE_M4BS:
            dst = os.path.join(self.untagged, stem, stem + ".m4b")
            self.assertTrue(os.path.isfile(dst), "missing " + dst)
            self.assertFalse(os.path.exists(os.path.join(self.imp, stem + ".m4b")))

    def test_bare_mp3_goes_to_intake_bare(self):
        self.assertTrue(os.path.isfile(
            os.path.join(self.intake, "AManCalledOveUnabridged.mp3")))
        self.assertFalse(os.path.isdir(
            os.path.join(self.intake, "AManCalledOveUnabridged")))

    def test_mp3_folders_move_whole_with_art(self):
        for folder, files in self.MP3_FOLDERS.items():
            dst = os.path.join(self.intake, folder)
            self.assertTrue(os.path.isdir(dst), "missing " + dst)
            self.assertEqual(sorted(os.listdir(dst)), sorted(files))   # jpg rides along
            self.assertFalse(os.path.exists(os.path.join(self.imp, folder)))

    def test_collection_split_into_individual_books(self):
        for b in self.COLLECTION_BOOKS:
            dst = os.path.join(self.untagged, b, b + ".m4b")
            self.assertTrue(os.path.isfile(dst), "missing " + dst)
        # sidecar-only leftover (cover.jpg) → the source folder is removed
        self.assertFalse(os.path.exists(os.path.join(self.imp, self.COLLECTION)))

    def test_nested_single_dir_renamed_to_top_name(self):
        dst = os.path.join(self.intake, "Lord Of The Flies")
        self.assertTrue(os.path.isdir(dst))
        self.assertEqual(sorted(os.listdir(dst)), ["01.mp3", "02.mp3", "03.mp3"])
        self.assertFalse(os.path.isdir(os.path.join(dst, "_")))
        self.assertFalse(os.path.exists(os.path.join(self.imp, "Lord Of The Flies")))

    def test_flac_left_for_music_untouched(self):
        self.assertTrue(os.path.isfile(os.path.join(self.imp, "loose song.flac")))
        self.assertTrue(os.path.isfile(os.path.join(self.imp, "Some Flac Album", "a.flac")))
        self.assertTrue(os.path.isfile(os.path.join(self.imp, "Some Flac Album", "b.flac")))

    def test_single_m4b_folder_takes_longer_name(self):
        # folder name richer than the file stem → book named after the folder,
        # file renamed to match
        book = "Some Great Book (2019)"
        self.assertTrue(os.path.isfile(os.path.join(self.untagged, book, book + ".m4b")))
        self.assertFalse(os.path.exists(os.path.join(self.imp, book)))
        # file stem richer than the folder name → the stem wins
        book2 = "Harry Potter and the Deathly Hallows"
        self.assertTrue(os.path.isfile(os.path.join(self.untagged, book2, book2 + ".m4b")))
        self.assertFalse(os.path.exists(os.path.join(self.imp, "hp7")))

    def test_mixed_folder_m4b_out_and_mp3_remainder_converts(self):
        self.assertTrue(os.path.isfile(
            os.path.join(self.untagged, "Mixed Book", "Mixed Book.m4b")))
        dst = os.path.join(self.intake, "Mixed Book")
        self.assertTrue(os.path.isdir(dst))
        self.assertEqual(sorted(os.listdir(dst)), ["p1.mp3", "p2.mp3"])
        self.assertFalse(os.path.exists(os.path.join(self.imp, "Mixed Book")))

    def test_multi_disc_flattened_with_disc_prefixes(self):
        dst = os.path.join(self.intake, "Multi Disc Book")
        self.assertTrue(os.path.isdir(dst))
        self.assertEqual(sorted(os.listdir(dst)),
                         ["CD1 - 01.mp3", "CD1 - 02.mp3", "CD2 - 01.mp3", "CD2 - 02.mp3"])
        self.assertFalse(os.path.exists(os.path.join(self.imp, "Multi Disc Book")))

    def test_import_dir_residue(self):
        # only the fresh entry and the music hand-offs remain
        self.assertEqual(sorted(os.listdir(self.imp)),
                         ["Fresh Book.m4b", "Some Flac Album", "loose song.flac"])

    # ── imports_waiting on the replica ────────────────────────────────────────

    def test_imports_waiting_counts(self):
        self.assertEqual(self.waiting_before, self.N_AUDIOBOOK_ENTRIES)
        self.assertEqual(self.waiting_after, 1)   # the fresh entry; FLAC is music, not waiting


class TestImportsWaitingAndStatus(unittest.TestCase):
    def test_status_folds_waiting_into_dropped(self):
        td = tempfile.mkdtemp(prefix="ab-status-")
        self.addCleanup(shutil.rmtree, td, True)
        imp = os.path.join(td, "imports")
        temp = os.path.join(td, "ab-temp")
        review = os.path.join(td, "review")
        os.makedirs(os.path.join(temp, "recentlyadded"))
        os.makedirs(os.path.join(temp, "untagged"))
        _mk(temp, os.path.join("recentlyadded", "converting.mp3"))
        _mk(imp, "Waiting Book.m4b")
        _mk(imp, os.path.join("Waiting Folder", "01.mp3"))
        _mk(imp, os.path.join("Waiting Folder", "02.mp3"))
        _mk(imp, "song.flac")                       # music-shaped: never 'waiting'
        with mock.patch.object(ab, "DATA_DIR", os.path.join(td, "data")):
            self.assertEqual(ab.imports_waiting(imp), 2)
            st = ab.organizer_status(temp, td, review_dir=review, import_dir=imp)
            self.assertEqual(st["imports_waiting"], 2)
            self.assertEqual(st["dropped"], 3)      # 1 in recentlyadded + 2 waiting
            st2 = ab.organizer_status(temp, td, review_dir=review)
            self.assertEqual(st2["imports_waiting"], 0)
            self.assertEqual(st2["dropped"], 1)

    def test_missing_import_dir_is_zero(self):
        self.assertEqual(ab.imports_waiting("/nonexistent/plexify-imports-xyz"), 0)




class TestReviewFindingFixes(unittest.TestCase):
    """Regression tests for the adversarial-review findings (2026-07-10)."""

    def test_author_series_title_keeps_real_title(self):
        # narrator drop must NOT fire without a (year) anchor — 'The Waste Lands' is the title
        g = ab.infer_book_guess("Stephen King - The Dark Tower - The Waste Lands.m4b", {})
        self.assertEqual(g["author"], "Stephen King")
        self.assertIn("Waste Lands", g["title"])

    def test_narrator_drop_still_works_with_year(self):
        g = ab.infer_book_guess(
            "Alexandre Dumas - The Count of Monte Cristo (2008) - John Lee.m4b", {})
        self.assertEqual((g["title"], g["author"]),
                         ("The Count of Monte Cristo", "Alexandre Dumas"))

    def test_lowercase_by_phrases_keep_full_title(self):
        for name in ("Seduced by the Highlander.m4b", "History by the Numbers.m4b",
                     "Death by the Riverside.m4b"):
            g = ab.infer_book_guess(name, {})
            self.assertIsNone(g["author"], name)
            self.assertEqual(g["title"], name[:-4].replace("_", " "), name)

    def test_camelcase_one_worders_survive(self):
        self.assertEqual(ab.infer_book_guess("ReWork.m4b", {})["title"], "ReWork")
        self.assertEqual(ab.infer_book_guess("SuperFreakonomics.m4b", {})["title"],
                         "SuperFreakonomics")
        # tags are never camel-split
        g = ab.infer_book_guess("x.m4b", {"album": "SuperFreakonomics"})
        self.assertEqual(g["title"], "SuperFreakonomics")
        # real run-together names still split
        self.assertEqual(ab.infer_book_guess("AManCalledOveUnabridged.mp3", {})["title"],
                         "A Man Called Ove")

    def test_free_slot_same_second_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for _ in range(3):
                p = ab._free_slot(d, "Book.m4b")
                open(p, "w").write("x")
                paths.append(p)
            self.assertEqual(len(set(paths)), 3)

    def _aged_tree(self, d):
        t = time.time() - 600
        for dp, dns, fns in os.walk(d, topdown=False):
            for n in fns + dns:
                os.utime(os.path.join(dp, n), (t, t))
        os.utime(d, (t, t))

    def _mk(self, root, rel, data=b"x"):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def _dirs(self):
        base = tempfile.mkdtemp()
        imp = os.path.join(base, "imports"); os.makedirs(imp)
        temp = os.path.join(base, "temp")
        os.makedirs(os.path.join(temp, "recentlyadded"))
        os.makedirs(os.path.join(temp, "untagged"))
        return base, imp, temp

    def test_author_folder_of_books_not_merged(self):
        base, imp, temp = self._dirs()
        try:
            self._mk(imp, "Brandon Sanderson/Mistborn/01.mp3")
            self._mk(imp, "Brandon Sanderson/Mistborn/02.mp3")
            self._mk(imp, "Brandon Sanderson/Elantris/01.mp3")
            self._aged_tree(imp)
            ab._route_size_memo.clear()
            ab.route_imports(imp, temp)
            out = ab.route_imports(imp, temp)
            self.assertEqual(out["to_convert"], 2)
            intake = sorted(os.listdir(os.path.join(temp, "recentlyadded")))
            self.assertEqual(intake, ["Brandon Sanderson - Elantris",
                                      "Brandon Sanderson - Mistborn"])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_disc_folders_still_flatten_to_one_book(self):
        base, imp, temp = self._dirs()
        try:
            self._mk(imp, "Dune/CD1/01.mp3")
            self._mk(imp, "Dune/CD2/01.mp3")
            self._aged_tree(imp)
            ab._route_size_memo.clear()
            ab.route_imports(imp, temp)
            out = ab.route_imports(imp, temp)
            self.assertEqual(out["to_convert"], 1)
            book = os.path.join(temp, "recentlyadded", "Dune")
            self.assertEqual(sorted(os.listdir(book)), ["CD1 - 01.mp3", "CD2 - 01.mp3"])
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_companion_pdf_preserved_not_deleted(self):
        base, imp, temp = self._dirs()
        try:
            self._mk(imp, "Project Hail Mary/Project Hail Mary.m4b")
            self._mk(imp, "Project Hail Mary/Project Hail Mary.pdf")
            self._aged_tree(imp)
            ab._route_size_memo.clear()
            ab.route_imports(imp, temp)
            ab.route_imports(imp, temp)
            book = os.path.join(temp, "untagged", "Project Hail Mary")
            self.assertTrue(os.path.isfile(os.path.join(book, "Project Hail Mary.m4b")))
            self.assertTrue(os.path.isfile(os.path.join(book, "Project Hail Mary.pdf")))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_symlinked_entry_skipped(self):
        base, imp, temp = self._dirs()
        try:
            outside = os.path.join(base, "outside"); os.makedirs(outside)
            self._mk(base, "outside/x.mp3")
            os.symlink(outside, os.path.join(imp, "linked"))
            self._aged_tree(imp)
            ab._route_size_memo.clear()
            ab.route_imports(imp, temp)
            out = ab.route_imports(imp, temp)
            self.assertEqual(out["to_convert"], 0)
            self.assertTrue(os.path.isfile(os.path.join(outside, "x.mp3")))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_route_memo_pruned_after_route(self):
        base, imp, temp = self._dirs()
        try:
            self._mk(imp, "Solo Book.m4b")
            self._aged_tree(imp)
            ab._route_size_memo.clear()
            ab.route_imports(imp, temp)
            ab.route_imports(imp, temp)     # routes + prunes on next pass
            ab.route_imports(imp, temp)
            self.assertFalse(any("Solo Book" in k for k in ab._route_size_memo))
        finally:
            shutil.rmtree(base, ignore_errors=True)

    def test_cleanup_book_dir_moves_companions_to_library(self):
        with tempfile.TemporaryDirectory() as d:
            book = os.path.join(d, "book"); os.makedirs(book)
            dest = os.path.join(d, "lib"); os.makedirs(dest)
            open(os.path.join(book, "b.pdf"), "w").write("x")
            open(os.path.join(book, "b.chapters.txt"), "w").write("x")
            ab._cleanup_book_dir(book, dest)
            self.assertTrue(os.path.isfile(os.path.join(dest, "b.pdf")))
            self.assertFalse(os.path.exists(book))



class TestClobberFixes(unittest.TestCase):
    """The 2026-07-10 live clobber: series-named album tags matched 3 books to one product,
    and _tag_and_file overwrote the shared destination twice."""

    def test_series_album_tag_overridden_by_structured_filename(self):
        g = ab.infer_book_guess("Iron Gold by Pierce Brown Book 4.m4b",
                                {"album": "Red Rising", "albumartist": "Pierce Brown"})
        self.assertEqual((g["title"], g["author"]), ("Iron Gold", "Pierce Brown"))
        # the tag reading survives as the fallback interpretation
        self.assertIn({"title": "Red Rising", "author": "Pierce Brown"}, g.get("alts") or [])

    def test_agreeing_tag_stays_primary(self):
        g = ab.infer_book_guess("Red Rising by Pierce Brown Book 1.m4b",
                                {"album": "Red Rising", "albumartist": "Pierce Brown"})
        self.assertEqual(g["title"], "Red Rising")

    def test_blob_filename_keeps_tag_priority(self):
        g = ab.infer_book_guess("whatever rip 001.m4b",
                                {"album": "The Martian", "albumartist": "Andy Weir"})
        self.assertEqual(g["title"], "The Martian")

    def test_tag_and_file_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            lib = os.path.join(d, "lib")
            dest_dir = os.path.join(lib, "Pierce Brown", "Red Rising")
            os.makedirs(dest_dir)
            open(os.path.join(dest_dir, "Red Rising.m4b"), "w").write("EXISTING")
            src = os.path.join(d, "incoming.m4b")
            open(src, "w").write("NEWBOOK")
            meta = {"asin": "B0", "title": "Red Rising", "authors": ["Pierce Brown"],
                    "narrators": [], "release_date": "", "summary": "", "image": "",
                    "genres": [], "series": "", "series_position": ""}
            with mock.patch.object(ab, "apply_tags"),                  mock.patch.object(ab, "_fetch_cover", return_value=None):
                with self.assertRaises(ab.DestConflictError):
                    ab._tag_and_file(src, meta, lib)
            # nothing was overwritten, the incoming file is untouched
            self.assertEqual(open(os.path.join(dest_dir, "Red Rising.m4b")).read(), "EXISTING")
            self.assertTrue(os.path.isfile(src))




class TestManualResolveEnrichment(unittest.TestCase):
    """Manual review resolves must borrow the matched product's cover/summary while keeping
    the human's author/title authoritative (they shipped with no cover before)."""

    def test_manual_resolve_fetches_cover_keeps_user_values(self):
        with tempfile.TemporaryDirectory() as d:
            review = os.path.join(d, "review"); os.makedirs(review)
            lib = os.path.join(d, "lib"); os.makedirs(lib)
            open(os.path.join(review, "Renegat.m4b"), "w").write("x")
            captured = {}
            def fake_apply(path, meta, cover=None):
                captured["meta"] = dict(meta); captured["cover"] = cover
            with mock.patch.object(ab, "search_and_pick",
                                   return_value=({"asin": "B0TEST"}, 88, [], {})), \
                 mock.patch.object(ab, "fetch_audnexus",
                                   return_value={"asin": "B0TEST", "title": "WRONG PRODUCT TITLE",
                                                 "authors": ["Wrong Author"], "narrators": ["N"],
                                                 "summary": "S", "image": "http://img",
                                                 "genres": ["Sci-Fi"], "series": "",
                                                 "series_position": "", "release_date": ""}), \
                 mock.patch.object(ab, "_fetch_cover", return_value=b"IMG"), \
                 mock.patch.object(ab, "apply_tags", side_effect=fake_apply):
                res = ab.resolve_book("Renegat.m4b", review, lib,
                                      author="Orson Scott Card", title="Renegat")
            self.assertTrue(res["ok"], res)
            self.assertEqual(captured["meta"]["title"], "Renegat")
            self.assertEqual(captured["meta"]["authors"], ["Orson Scott Card"])
            self.assertEqual(captured["meta"]["image"], "http://img")
            self.assertEqual(captured["cover"], b"IMG")

    def test_manual_resolve_still_works_offline(self):
        with tempfile.TemporaryDirectory() as d:
            review = os.path.join(d, "review"); os.makedirs(review)
            lib = os.path.join(d, "lib"); os.makedirs(lib)
            open(os.path.join(review, "Obscure.m4b"), "w").write("x")
            with mock.patch.object(ab, "search_and_pick", return_value=(None, 0, [], {})), \
                 mock.patch.object(ab, "_fetch_cover", return_value=None), \
                 mock.patch.object(ab, "apply_tags"):
                res = ab.resolve_book("Obscure.m4b", review, lib,
                                      author="Nobody", title="Obscure")
            self.assertTrue(res["ok"], res)




class TestItunesCoverFallback(unittest.TestCase):
    def _session(self, results):
        s = mock.Mock()
        s.get.return_value.json.return_value = {"results": results}
        return s

    def test_author_gate_rejects_title_word_collisions(self):
        # 'Renegat' also matches a Harlan Ellison title on iTunes — wrong author, no cover
        s = self._session([
            {"artistName": "Harlan Ellison & Richard Gilliland",
             "artworkUrl100": "http://x/harlan.100x100.jpg"},
            {"artistName": "Orson Scott Card",
             "artworkUrl100": "http://x/osc.100x100.jpg"},
        ])
        url = ab.itunes_cover_search("Renegat", "Orson Scott Card", session=s)
        self.assertEqual(url, "http://x/osc.600x600.jpg")

    def test_no_results_returns_none(self):
        self.assertIsNone(ab.itunes_cover_search("Nope", "Nobody", session=self._session([])))




class TestReviewItems(unittest.TestCase):
    """The review queue must come from the review FOLDER, not the recent-records window —
    a busy day pushed 9 outstanding items past the window and the UI showed count-with-no-rows."""

    def test_folder_is_source_of_truth_with_ledger_join(self):
        with tempfile.TemporaryDirectory() as d:
            review = os.path.join(d, "review"); os.makedirs(review)
            open(os.path.join(review, "Waiting.m4b"), "w").write("x")
            open(os.path.join(review, "NeverLogged.m4b"), "w").write("x")
            with mock.patch.object(ab, "DATA_DIR", d):
                with open(os.path.join(d, ab.BOOKS_LEDGER), "w") as fh:
                    fh.write(json.dumps({"status": "review", "file": "Waiting.m4b",
                                         "reason": "no_candidates",
                                         "guess": {"title": "Waiting"},
                                         "candidates": [{"asin": "B1"}]}) + "\n")
                    # 40 organized records AFTER it — enough to push it out of any window
                    for i in range(40):
                        fh.write(json.dumps({"status": "organized", "file": f"o{i}.m4b"}) + "\n")
                items = ab.review_items(review)
            files = {i["file"]: i for i in items}
            self.assertIn("Waiting.m4b", files)
            self.assertEqual(files["Waiting.m4b"]["reason"], "no_candidates")
            self.assertEqual(files["Waiting.m4b"]["candidates"], [{"asin": "B1"}])
            self.assertIn("NeverLogged.m4b", files)   # present even without a ledger record

    def test_resolved_files_disappear(self):
        with tempfile.TemporaryDirectory() as d:
            review = os.path.join(d, "review"); os.makedirs(review)
            with mock.patch.object(ab, "DATA_DIR", d):
                with open(os.path.join(d, ab.BOOKS_LEDGER), "w") as fh:
                    fh.write(json.dumps({"status": "review", "file": "Gone.m4b",
                                         "reason": "no_candidates"}) + "\n")
                self.assertEqual(ab.review_items(review), [])


if __name__ == "__main__":
    unittest.main()
