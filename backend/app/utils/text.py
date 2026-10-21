"""Text helpers: slugs, short codes, query normalisation and synonym expansion."""
from __future__ import annotations

import re
import secrets
import unicodedata
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymongo.database import Database as MongoDatabase

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion when read aloud at the counter


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "item"


def short_code(prefix: str, length: int = 6) -> str:
    return prefix + "-" + "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


_WS = re.compile(r"\s+")
# Characters a query keeps besides letters, combining marks and digits: whitespace, hyphen, dot and slash.
_KEEP_PUNCT = frozenset("-./")
# Zero-width joiner and non-joiner: optional shaping hints in Kannada, Telugu and Devanagari text, typed by
# some keyboards and not others, so they are dropped to make both spellings of a word the same.
_JOINERS = dict.fromkeys(map(ord, "\u200c\u200d"))


@lru_cache(maxsize=4096)
def _query_char(c: str) -> str:
    """One character of a query after normalisation: letters, combining marks and digits of every script
    are kept (Unicode categories L, M and N; Python's word class leaves out combining vowel signs such as
    those of Kannada, Telugu and Devanagari), Latin letters are lower-cased, anything else becomes a space."""
    if c.isspace() or c in _KEEP_PUNCT:
        return c
    cat = unicodedata.category(c)[0]
    if cat == "L":
        return c.lower() if c.isascii() or unicodedata.name(c, "").startswith("LATIN ") else c
    return c if cat in "MN" else " "


def is_word(token: str) -> bool:
    """A token made only of letters and combining marks (any script): what spelling correction may fix."""
    return bool(token) and all(unicodedata.category(c)[0] in "LM" for c in token)


def normalize_query(q: str) -> str:
    q = unicodedata.normalize("NFKC", q).translate(_JOINERS)
    q = unicodedata.normalize("NFC", "".join(_query_char(c) for c in q)).strip()
    # normalise common unit spellings: "1 ltr" -> "1 l", "25 watt" -> "25w"
    q = re.sub(r"(\d+)\s*(ltr|litre|liter|lt)\b", r"\1 l", q)
    q = re.sub(r"(\d+)\s*(watt|watts)\b", r"\1w", q)
    q = re.sub(r"(\d+)\s*(mtr|metre|meter)s?\b", r"\1 m", q)
    return _WS.sub(" ", q).strip()


def expand_synonyms(q: str, db: MongoDatabase | None = None) -> list[str]:
    """Extra terms to OR into keyword search. Never replaces the user's own words.

    The entries live in the synonyms collection, managed by admins (app/services/synonyms.py; migration 5
    imported the 42 that used to be written here). They are read from a per-process cache that checks the
    synonyms' version every 30 seconds; db names the database (default: the configured one)."""
    from app.services.synonyms import cache, expand  # lazy: the service imports the database layer

    return expand(q, cache.table(db))


def word_parts(text: str) -> list[str]:
    """The runs of letters, combining marks and digits in text, of every script (Kannada, Telugu and
    Devanagari words keep their vowel signs and viramas); everything else separates them."""
    return "".join(c if unicodedata.category(c)[0] in "LMN" else " " for c in text).split()


def search_tokens(words: list[str]) -> list[str]:
    """Words safe to put in a MongoDB $text search: letters, combining marks and digits only (any script),
    so user input can never be read as a phrase ("...") or a negation (-word)."""
    out: list[str] = []
    for w in words:
        out.extend(part for part in word_parts(w) if len(part) >= 2)
    return list(dict.fromkeys(out))
