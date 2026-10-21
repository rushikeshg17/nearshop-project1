"""Kannada, Telugu and Devanagari queries (P5.2): normalisation keeps every character, tokens and the spelling
vocabulary include those scripts, and the keyword stage matches catalog items' local_terms."""
import json
import unicodedata

import pytest

from app.services import search
from app.services.search import SearchParams, run_search
from app.utils.text import is_word, normalize_query, search_tokens
from database.migrations import m0004_local_terms as m4
from tests.conftest import CENTER

CHARGER_KN = "ಚಾರ್ಜರ್"  # Kannada for charger: three fragments before P5.2
CHARGER_HI = "चार्जर"
CHARGER_TE = "ఛార్జర్"

# Strings that must come through normalisation unchanged apart from spacing.
INDIC_STRINGS = [
    CHARGER_KN, CHARGER_HI, CHARGER_TE,
    "ಮೊಬೈಲ್ ಚಾರ್ಜರ್", "ಇಸ್ತ್ರಿ ಪೆಟ್ಟಿಗೆ", "ಬೈಕ್ ಆಯಿಲ್", "ಹೆಲ್ಮೆಟ್", "ನಲ್ಲಿ", "ಛತ್ರಿ", "ಪೊರಕೆ", "ಸೊಳ್ಳೆ",
    "బైక్ హెల్మెట్", "ఇస్త్రీ పెట్టె", "కుళాయి", "గొడుగు", "చీపురు", "తువ్వాలు",
    "मिक्सर ग्राइंडर", "इंजन ऑयल", "बाल्टी", "छाता", "टोंटी", "मच्छर", "तौलिया", "क्षत्रिय",
    "ಬ್ಯಾಟರಿ ೨ ಸೆಲ್", "बैटरी ४ पैक",  # native digits
]


def squash(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).split())


# ---------------------------------------------------------------- normalisation

@pytest.mark.parametrize("text", INDIC_STRINGS)
def test_indic_strings_survive_normalisation(text):
    assert normalize_query(text) == squash(text)
    assert normalize_query(f"  {text.replace(' ', '   ')} \t") == squash(text)  # only spacing changes


def test_every_test_string_survives():
    survived = [t for t in INDIC_STRINGS if normalize_query(t) == squash(t)]
    assert len(survived) == len(INDIC_STRINGS)  # 100%


def test_charger_example_is_one_token():
    for word in (CHARGER_KN, CHARGER_HI, CHARGER_TE):
        assert normalize_query(word) == word
        assert search_tokens([normalize_query(word)]) == [word]
        assert is_word(word)
    assert search_tokens(["ಬೈಕ್ ಆಯಿಲ್"]) == ["ಬೈಕ್", "ಆಯಿಲ್"]


def test_punctuation_still_separates_and_latin_is_lower_cased():
    assert normalize_query("ಚಾರ್ಜರ್!!  (Type-C)") == "ಚಾರ್ಜರ್ type-c"
    assert normalize_query("चार्जर, 25 Watt") == "चार्जर 25w"
    assert normalize_query("CAFÉ Ünïcode") == "café ünïcode"
    assert normalize_query('"phone" -charger') == "phone -charger"
    assert search_tokens([normalize_query('"phone" -charger')]) == ["phone", "charger"]


def test_nfc_and_joiners():
    # A decomposed vowel sign (Kannada o = e + length mark) is composed; zero-width joiners are dropped.
    decomposed = "ಕೊ"
    assert normalize_query(decomposed) == unicodedata.normalize("NFC", decomposed) == "ಕೊ"
    assert normalize_query("क्‍ष") == "क्ष"
    assert normalize_query("ಚಾರ್‌ಜರ್") == CHARGER_KN


def test_english_queries_unchanged():
    assert normalize_query("  Phone CHARGER 1 Litre ") == "phone charger 1 l"
    assert search_tokens(["usb-c", "a", "led 9w"]) == ["usb", "led", "9w"]


def test_underscore_now_separates_words():
    # A deliberate change: the underscore is punctuation (Unicode category Pc), not a letter, mark or digit,
    # so it now splits words. Before P5.2 the word class kept it, giving the single token "usb_c".
    assert normalize_query("usb_c cable") == "usb c cable"
    assert search_tokens([normalize_query("usb_c cable")]) == ["usb", "cable"]


# ---------------------------------------------------------------- local_terms data and migration

def test_local_terms_file_is_valid():
    terms = m4.load_local_terms()
    catalog = {c["slug"] for c in json.loads((m4.DATA_FILE.parent / "catalog.json").read_text())}
    assert terms and set(terms) <= catalog
    for slug, values in terms.items():
        assert all(normalize_query(v) == v for v in values), slug
        scripts = {unicodedata.name(c).split()[0] for v in values for c in v if not c.isspace() and not c.isascii()}
        assert {"KANNADA", "TELUGU", "DEVANAGARI"} <= scripts, slug


def test_load_local_terms_normalises_and_rejects_bad_files(tmp_path):
    f = tmp_path / "local_terms.json"
    f.write_text(json.dumps({"items": [{"slug": "x", "kn": ["  ಮೊಬೈಲ್   ಚಾರ್ಜರ್ "], "latin": ["Charjar", "charjar"]},
                                       {"slug": "empty", "kn": []}]}), encoding="utf-8")
    assert m4.load_local_terms(f) == {"x": ["ಮೊಬೈಲ್ ಚಾರ್ಜರ್", "charjar"]}
    f.write_text(json.dumps({"items": [{"slug": "x", "kn": "ಚಾರ್ಜರ್"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        m4.load_local_terms(f)
    f.write_text(json.dumps([]), encoding="utf-8")
    with pytest.raises(ValueError):
        m4.load_local_terms(f)


def test_local_terms_past_the_validator_limits_are_refused(tmp_path, db, ids):
    # The migration writes before ensure_schema applies the validator, so it checks the limits itself:
    # a list past them would make every later stock or price update to the listing fail validation.
    f = tmp_path / "local_terms.json"
    f.write_text(json.dumps({"items": [{"slug": "x", "latin": ["a" * (m4.MAX_TERM_LENGTH + 1)]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="longer than"):
        m4.load_local_terms(f)
    f.write_text(json.dumps({"items": [{"slug": "x", "latin": [f"t{n}" for n in range(m4.MAX_TERMS + 1)]}]}),
                 encoding="utf-8")
    with pytest.raises(ValueError, match="at most"):
        m4.load_local_terms(f)
    with pytest.raises(ValueError):
        m4.apply_local_terms(db.mongo, {"samsung-25w-charger": ["x" * (m4.MAX_TERM_LENGTH + 1)]})
    assert "local_terms" not in db.mongo.catalog_items.find_one({"slug": "samsung-25w-charger"})


@pytest.fixture
def charger_terms(db, ids):
    """The test catalogue's charger, named in Kannada, Telugu and Hindi on the item and its listing."""
    terms = {"samsung-25w-charger": [CHARGER_KN, "ಮೊಬೈಲ್ ಚಾರ್ಜರ್", CHARGER_TE, CHARGER_HI, "charjar"]}
    yield m4.apply_local_terms(db.mongo, terms, batch_size=1)
    db.mongo.catalog_items.update_many({}, {"$unset": {"local_terms": ""}})
    db.mongo.products.update_many({}, {"$unset": {"local_terms": ""}})
    search.reset_vocabulary()


def test_migration_copies_terms_to_listings_and_is_idempotent(db, ids, charger_terms):
    assert charger_terms == (1, 1)
    listing = db.mongo.products.find_one({"_id": ids["charger"]})
    assert listing["local_terms"][0] == CHARGER_KN
    assert "local_terms" not in db.mongo.products.find_one({"_id": ids["tape"]})
    terms = {"samsung-25w-charger": listing["local_terms"]}
    assert m4.apply_local_terms(db.mongo, terms) == (0, 0)  # nothing left to do
    m4.apply(db.mongo)  # the file's slugs are not in the test catalogue: nothing changes, nothing fails
    assert db.mongo.products.find_one({"_id": ids["charger"]})["local_terms"] == listing["local_terms"]
    for collection in ("catalog_items", "products"):
        index = db.mongo[collection].index_information()["local_terms"]
        assert index["key"] == [("local_terms", 1)]


def test_validator_rejects_bad_local_terms(db, ids):
    from pymongo.errors import WriteError

    with pytest.raises(WriteError):
        db.mongo.products.update_one({"_id": ids["tape"]}, {"$set": {"local_terms": [""]}})
    with pytest.raises(WriteError):
        db.mongo.catalog_items.update_one({"slug": "teflon-tape"}, {"$set": {"local_terms": "ನಲ್ಲಿ"}})


# ---------------------------------------------------------------- search

def search_keys(db, q: str) -> list[int | None]:
    res = run_search(db, SearchParams(q=q, lat=CENTER[0], lng=CENTER[1], radius_km=5), log=False)
    return [g["catalog_item_id"] for g in res["groups"]]


def charger_item(db) -> int:
    return db.mongo.catalog_items.find_one({"slug": "samsung-25w-charger"})["_id"]


@pytest.mark.parametrize("q", [CHARGER_KN, f"  {CHARGER_KN}!", "ಮೊಬೈಲ್ ಚಾರ್ಜರ್", CHARGER_TE, CHARGER_HI, "Charjar"])
def test_local_term_query_returns_the_item(db, charger_terms, q):
    assert search_keys(db, q)[:1] == [charger_item(db)]


def test_kannada_query_without_local_terms_finds_nothing(db, ids):
    assert charger_item(db) not in search_keys(db, CHARGER_KN)


def test_listing_without_copied_terms_matches_through_its_catalog_item(db, charger_terms, ids):
    # A listing created from the catalogue after the migration ran has no local_terms of its own.
    db.mongo.products.update_one({"_id": ids["charger"]}, {"$unset": {"local_terms": ""}})
    assert search_keys(db, CHARGER_KN)[:1] == [charger_item(db)]


def test_local_term_weight_equals_a_brand_match(db, charger_terms, ids):
    scores = search.local_term_scores(db, f"{CHARGER_KN} ಮೊಬೈಲ್ ಚಾರ್ಜರ್")
    assert scores == {ids["charger"]: 2 * search.W_LOCAL_TERM}  # two terms: the word and the phrase
    assert search.W_LOCAL_TERM == 6.0  # the products text index's brand weight (database/schema.py)
    assert search.local_term_scores(db, "ಚಾರ್ಜ") == {}  # exact tokens only, no prefix match


def test_vocabulary_has_indic_words_and_corrects_them(db, charger_terms):
    search.reset_vocabulary()
    vocab = search._vocabulary(db)
    assert {CHARGER_KN, CHARGER_TE, CHARGER_HI, "charger"} <= vocab
    # A Kannada misspelling (long a dropped) is corrected to the local term, and the search finds the item.
    assert search.suggest_correction(db, "ಚರ್ಜರ್") == CHARGER_KN
    res = run_search(db, SearchParams(q="ಚರ್ಜರ್", lat=CENTER[0], lng=CENTER[1], radius_km=5), log=False)
    assert res["corrected_from"] == "ಚರ್ಜರ್" and res["groups"][0]["catalog_item_id"] == charger_item(db)


def test_english_search_unchanged(db, charger_terms, ids):
    assert search_keys(db, "phone charger")[:1] == [charger_item(db)]
    assert ids["charger"] not in search.local_term_scores(db, "phone charger")
