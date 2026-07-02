"""Demo/Live version matching for liked-song acquisition.

User rules:
 - (2026-06-13) a downloaded song whose title carries '(demo)' WITHIN PARENTHESES
   may not fulfil a liked song UNLESS that liked song's own title also carries
   '(demo)'. Paren-specific so 'Demolition' / 'Demonstrate' never trip.
 - (2026-06-15) a downloaded song with the WORD 'live' ANYWHERE in its title may
   not fulfil a liked song UNLESS that liked song's title also has the word 'live'.
   Whole-word matched, so 'Alive' / 'Olive' / 'Delivery' / 'Lively' / 'Clive' /
   'Living' never trip; a real studio song literally titled with the word 'Live'
   ('Live and Let Die', 'Long Live', 'Live Forever') is spared because its liked
   counterpart also contains the word, so the ban never fires for it.
"""
import re

# 'live' as a standalone word, anywhere in the title (2026-06-15 broadening).
_LIVE_WORD = re.compile(r"\blive\b", re.I)
# 'demo' stays parenthetical-only on purpose.
_PAREN_DEMO = re.compile(r"[\(\[][^)\]]*\bdemo\b[^)\]]*[\)\]]", re.I)
# Legacy combined parenthetical marker (demo OR live in parens) — kept for the
# album/folder-level heuristics that must stay conservative (a studio album named
# "Live Through This" must not nuke its own tracks).
_PAREN_DL = re.compile(r"[\(\[][^)\]]*\b(?:demo|live)\b[^)\]]*[\)\]]", re.I)


def has_live(title: str) -> bool:
    """True if the title contains the standalone word 'live' anywhere."""
    return bool(_LIVE_WORD.search(title or ""))


def has_demo(title: str) -> bool:
    """True if the title has a (…demo…) / [..demo..] parenthetical group."""
    return bool(_PAREN_DEMO.search(title or ""))


def paren_demo_live(title: str) -> bool:
    """Legacy conservative marker: a (demo)/(live) parenthetical group ONLY.
    Used for album/folder-name checks where a whole-word 'live' would
    false-positive on studio albums titled with the word (e.g. 'Live Through This')."""
    return bool(_PAREN_DL.search(title or ""))


def demo_live_banned(candidate_title: str, liked_title: str) -> bool:
    """Song-title rule. True = this candidate may NOT fulfil the liked song because:
      • it is a 'live' take (the word 'live' appears anywhere) and the liked song is
        not, OR
      • it is a '(demo)' take and the liked song is not.
    Live and demo are judged independently, each with its own 'unless the liked
    counterpart is the same kind of take' escape hatch."""
    c = candidate_title or ""
    l = liked_title or ""
    if has_live(c) and not has_live(l):
        return True
    if has_demo(c) and not has_demo(l):
        return True
    return False
