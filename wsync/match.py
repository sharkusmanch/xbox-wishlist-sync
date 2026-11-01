"""Title normalization and confident matching (pure, no I/O).

A wrong confident match can remove a wanted game from a list, so matching is
strict: an exact normalized title, or an exact match once edition noise is
removed. There is no fuzzy matching.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

_QUOTES = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u00b4": "'", "`": "'", "\u02bc": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"',
}


def norm(title: str) -> str:
    s = title
    for ch, rep in _QUOTES.items():
        s = s.replace(ch, rep)
    # Drop these before NFKD, which would otherwise turn the trademark sign into "TM".
    s = re.sub(r"[\u2122\u00ae\u00a9]", "", s).lower()
    # Delta is a Greek letter, not a combining mark, so NFKD would keep it.
    s = s.replace("Δ", " delta ").replace("δ", " delta ")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ").replace("'", "")
    s = re.sub(r"[\W_]+", " ", s)
    return s.strip()


_NOISE_PHRASES = [
    "premium plus edition",
    "premium edition",
    "digital deluxe edition",
    "deluxe edition",
    "gold edition",
    "ultimate edition",
    "complete edition",
    "standard edition",
    "game of the year edition",
    "goty edition",
    "definitive edition",
    "enhanced",
    "for xbox series x|s",
    "for xbox one",
    "(xbox series x|s)",
    "(xbox one)",
    "xbox one x enhanced",
    "xbox series x|s optimized",
]
# Matched against the already-normalized title, so normalize the phrases the same way.
_NOISE = sorted({norm(p) for p in _NOISE_PHRASES}, key=lambda p: (-len(p), p))


def base_title(title: str) -> str:
    """`norm` with edition noise removed from the end of the title only.

    Noise is a trailing suffix, stripped repeatedly ("Foo Deluxe Edition for Xbox
    One" -> "foo"). Words in the middle of a title are part of the name.
    """
    n = norm(title)
    while True:
        for phrase in _NOISE:
            if n.endswith(f" {phrase}"):
                n = n[: -len(phrase)].rstrip()
                break
        else:
            return n


# The same phrases as _NOISE, as patterns over the original text: case-insensitive, after
# whitespace or a separator, at the very end.
_NOISE_TAIL = [
    re.compile(r"[\s\-:\u2013\u2014]+" + re.escape(p) + r"\s*$", re.I)
    for p in sorted(_NOISE_PHRASES, key=lambda p: (-len(p), p))
]


def strip_edition_noise(title: str) -> str:
    """The title with trailing edition noise removed, keeping its case and punctuation.

    The text counterpart of `base_title`, for building search queries. Never strips a
    title down to nothing.
    """
    out = title.strip()
    while True:
        for pattern in _NOISE_TAIL:
            stripped = pattern.sub("", out)
            if stripped != out and stripped.strip():
                out = stripped.rstrip(" -:,\u2013\u2014")
                break
        else:
            return out


@dataclass
class Match:
    status: Literal["matched", "ambiguous", "none"]
    target: str | None = None
    candidates: list[str] = field(default_factory=list)


def pick(item_title: str, candidates: list[tuple[str, str]]) -> Match:
    """Match a wishlist title to (title, target id) candidates.

    `candidates` on the result are candidate titles: the matched title, every
    candidate considered when ambiguous, or empty when there were none.
    """
    if not candidates:
        return Match("none")
    all_titles = [t for t, _ in candidates]
    item_norm = norm(item_title)
    item_base = base_title(item_title)
    if not item_norm:
        return Match("ambiguous", None, all_titles)

    for key, item_key in ((norm, item_norm), (base_title, item_base)):
        hits = [(t, tid) for t, tid in candidates if key(t) == item_key]
        if len(hits) == 1:
            return Match("matched", hits[0][1], [hits[0][0]])
        if len(hits) > 1:
            return Match("ambiguous", None, all_titles)
    return Match("ambiguous", None, all_titles)
