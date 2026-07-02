"""Local movie/TV title DB (from TMDB daily exports, built by /data/build_title_db.py)
used to recognize the film/show a Various-Artists soundtrack album is FOR, so two
VA albums for the same title can be combined. No network at runtime.

Two title sets:
  titles - all movie+tv normalized titles (exact/substring soundtrack matching).
  tv     - popular MULTI-WORD tv-show titles; an album that STARTS WITH one of these
           groups under the show, so a show's many albums (incl. its movie + comp
           albums) merge - e.g. all "Phineas and Ferb*" albums. Single-word shows are
           deliberately excluded (avoids "Lost in Translation" -> "Lost").
"""
import json, re, unicodedata, logging, time, os

log = logging.getLogger(__name__)
_DB = None          # frozenset of all normalized titles
_TV = None          # frozenset of popular multi-word tv-show titles
_DB_TS = 0.0
_DB_PATH = "/data/movie_tv_titles.json"

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8", "ix": "9"}


def norm_title(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return " ".join(_ROMAN.get(w, w) for w in t.split())


_BOILER = [
    r"music from and inspired by the motion picture",
    r"soundtrack from and inspired by the motion picture",
    r"songs from and inspired by the motion picture",
    r"original songs from the motion picture",
    r"music from the original motion picture",
    r"music from the motion picture", r"songs from the motion picture",
    r"the original motion picture soundtrack", r"original motion picture soundtrack",
    r"original motion picture score", r"the motion picture soundtrack", r"motion picture soundtrack",
    r"music from the (?:original )?(?:television|tv) series",
    r"music featured in the (?:netflix )?(?:tv )?series",
    r"music from the (?:netflix|apple original|amazon|disney|hbo max|hbo) (?:original )?(?:film|series)",
    r"soundtrack from the (?:netflix|apple original|amazon|disney|hbo) (?:original )?film",
    r"soundtrack from the (?:motion picture|tv series|television series|series|film)",
    r"music from the (?:film|series)",
    r"original (?:broadway|off broadway|west end|london) cast recording",
    r"original (?:tv|television) cast recording",
    r"original cast recording", r"broadway cast recording", r"cast recording",
    r"the original soundtrack", r"original soundtrack", r"original score",
    r"(?:the )?(?:netflix|apple original|amazon|disney|hbo) (?:original )?(?:film|series)",
    r"\bthe album\b", r"\bthe movie\b",
    r"\bmusic from\b", r"\bsoundtrack\b", r"\bost\b",
    r"deluxe edition", r"expanded edition", r"special edition", r"\bdeluxe\b", r"\bexpanded\b",
    r"\bvolume\b ?\d*", r"\bvol\b ?\d*", r"\b(?:19|20)\d{2}\b",
]
_BOILER_RE = re.compile("|".join("(?:%s)" % p for p in _BOILER))
# Generic phrases that exist as obscure DB titles but must never be a soundtrack key.
_BLOCK = {"the musical", "the album", "the movie", "the motion picture", "the soundtrack",
          "the story", "the beginning", "the end", "the return", "the experience",
          "the collection", "the very best", "the complete", "the score", "the song",
          "the songs", "the theme", "original", "motion picture", "the original"}


def _load():
    global _DB, _TV, _DB_TS
    if _DB is not None and (time.time() - _DB_TS) < 86400:
        return _DB
    try:
        raw = json.load(open(_DB_PATH))
        if isinstance(raw, dict):
            _DB = frozenset(raw.get("titles") or [])
            _TV = frozenset(raw.get("tv") or [])
        else:
            _DB = frozenset(raw)
            _TV = frozenset()
    except Exception as e:
        log.warning("title_db: load failed (%s) - soundtrack matching disabled", e)
        _DB = frozenset()
        _TV = frozenset()
    _DB_TS = time.time()
    return _DB


def soundtrack_key(album_name: str):
    """The normalized film/show title this VA album is for, or None.
    1) TV-SHOW PREFIX: album starts with a popular multi-word show title -> group by
       the show (merges a show's movie + compilation + season albums together).
    2) MOVIE/EXACT: strip soundtrack boilerplate -> exact DB title match.
    3) SUBSTRING: longest >=3-word phrase of the core that's a known title AND covers
       >=60% of it (handles 'METRO BOOMIN PRESENTS <title>').
    Movie safety: distinct movie titles never collapse (Dark Knight != Dark Knight
    Rises, Shrek != Shrek 2) because movies require an exact title."""
    db = _load()
    if not db:
        return None
    full = norm_title(album_name)
    if not full:
        return None
    tv = _TV or frozenset()
    words = full.split()
    n = len(words)
    for size in range(n, 1, -1):
        prefix = " ".join(words[:size])
        if prefix in tv and prefix not in _BLOCK:
            return prefix
    core = re.sub(r"\s+", " ", _BOILER_RE.sub(" ", full)).strip()
    if core and core in db and core not in _BLOCK:
        return core
    cw = core.split()
    cn = len(cw)
    if cn >= 3:
        for size in range(cn, 2, -1):
            if size < 0.6 * cn:
                break
            for i in range(0, cn - size + 1):
                gram = " ".join(cw[i:i + size])
                if gram in db and gram not in _BLOCK:
                    return gram
    return None


def rebuild() -> int:
    """Refresh /data/movie_tv_titles.json from TMDB's free daily export dumps
    (no API key). Scheduled monthly. Returns total title count, or -1 on failure."""
    import gzip, io, urllib.request, datetime
    global _DB
    GENERIC = {"the", "music", "soundtrack", "score", "songs", "song", "live", "hits",
               "collection", "mix", "deluxe", "original", "film", "movie", "series",
               "cast", "album", "ost", "love", "home", "one", "two", "go", "it", "her",
               "up", "us", "we", "them", "life", "more", "now", "time", "day", "night"}
    TV_POP_MIN = 10.0

    def _fetch(kind):
        base = datetime.date.today()
        for back in range(0, 5):
            d = base - datetime.timedelta(days=back)
            url = "http://files.tmdb.org/p/exports/%s_%02d_%02d_%04d.json.gz" % (
                kind, d.month, d.day, d.year)
            try:
                raw = urllib.request.urlopen(url, timeout=90).read()
                return gzip.GzipFile(fileobj=io.BytesIO(raw))
            except Exception:
                continue
        return None

    titles, tv = set(), set()
    for kind, field in (("movie_ids", "original_title"), ("tv_series_ids", "original_name")):
        fh = _fetch(kind)
        if fh is None:
            log.warning("title_db.rebuild: %s export unreachable - keeping existing DB", kind)
            return -1
        for line in fh:
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("adult"):
                continue
            nt = norm_title(j.get(field) or "")
            if not nt or nt in GENERIC:
                continue
            wc = nt.count(" ") + 1
            pop = float(j.get("popularity") or 0)
            if wc >= 2 or (len(nt) >= 4 and pop >= 3.0):
                titles.add(nt)
            if kind == "tv_series_ids" and wc >= 2 and pop >= TV_POP_MIN:
                tv.add(nt)
    if len(titles) < 100000:
        log.warning("title_db.rebuild: only %d titles parsed - refusing to overwrite", len(titles))
        return -1
    tmp = _DB_PATH + ".tmp"
    json.dump({"titles": sorted(titles), "tv": sorted(tv)}, open(tmp, "w"))
    os.replace(tmp, _DB_PATH)
    _DB = None
    log.info("title_db.rebuild: %d titles, %d tv shows", len(titles), len(tv))
    return len(titles)
