"""Bilingual notifications and messages (P7.11): the catalogue in app/services/notification_text.py renders every
notification and bot reply in English and Kannada, the English word for word as before, in the recipient's language,
falling back to English where a Kannada text is missing. Every send goes through the fake WhatsApp client."""
import re
from datetime import timedelta
from string import Formatter

import pytest
from pymongo.errors import WriteError

from app.core.config import settings
from app.core.database import Database, utcnow
from app.services import notification_text as words
from app.services import reservations, whatsapp
from app.services.notification_text import MESSAGES, TEXTS, Term
from app.services.notifications import notify
from app.services.privacy import set_consent
from tests.conftest import HEADERS

KANNADA = re.compile(r"[ಀ-೿]")
CUSTOMER_PHONE = "9876543211"

# A value for every parameter any entry takes; a Term is rendered in the recipient's language.
PARAMS = {
    "code": "R-7KQ2MX", "customer": "Asha", "quantity": 2, "product": "Astral PTFE Thread Seal Tape", "minutes": 20,
    "shop": "Sri Ganesh Hardware", "reason": "Out of stock today", "summary": "2 x Tape, 1 x Charger",
    "address": "12 MG Road, Ballari", "distance_km": 1.4, "total": 249.5, "delivery_code": "0427",
    "items": "Tape: 3 (counted 4 days ago)", "name": "Tape", "days": 4, "before": 7, "line": "tape",
    "options": "1. Tape\n2. Duct Tape", "lines": "glue; nails", "count": 3, "max": 20, "error": "Not enough stock",
    "hint": "",
}


def placeholders(template: str | None) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template or "") if name}


# ---------------------------------------------------------------- the catalogue


@pytest.mark.parametrize("key", sorted(MESSAGES))
def test_every_message_renders_in_both_languages_with_its_parameters(key):
    entry = MESSAGES[key]
    if key in words.AWAITING_KANNADA:
        # The team's Kannada is still awaited (P9.1): English only, and a Kannada reader gets the English text.
        assert set(entry) == {"en"}, "a message with its Kannada added leaves AWAITING_KANNADA"
        assert words.render(key, "kn", PARAMS) == words.render(key, "en", PARAMS)
        assert "{" not in words.render(key, "en", PARAMS)[0] + (words.render(key, "en", PARAMS)[1] or "")
        return
    assert set(entry) == set(words.LANGUAGES), "every message has every language"
    for language in words.LANGUAGES:
        title, body = entry[language]
        # Each language takes the same parameters, so a caller never has to know which one it gets.
        assert placeholders(title) | placeholders(body) == (placeholders(entry["en"][0])
                                                           | placeholders(entry["en"][1]))
        rendered = words.render(key, language, PARAMS)
        assert rendered == (title.format(**PARAMS), body.format(**PARAMS) if body is not None else None)
        assert "{" not in rendered[0] and "{" not in (rendered[1] or "")
    kannada_title, kannada_body = words.render(key, "kn", PARAMS)
    assert KANNADA.search(kannada_title), "the Kannada title is Kannada"
    if entry["en"][1] not in (None, "{reason}"):
        assert KANNADA.search(kannada_body), "the Kannada body is Kannada"


@pytest.mark.parametrize("key", sorted(TEXTS))
def test_every_reply_and_fragment_renders_in_both_languages(key):
    entry = TEXTS[key]
    assert set(entry) == set(words.LANGUAGES)
    assert placeholders(entry["kn"]) == placeholders(entry["en"])
    for language in words.LANGUAGES:
        rendered = words.text(key, language, PARAMS)
        assert rendered == entry[language].format(**PARAMS) and "{" not in rendered
    if KANNADA.search(entry["kn"]) is None:  # a fragment of names and numbers only
        assert entry["kn"] == entry["en"] and not re.search(r"[A-Za-z]", re.sub(r"\{\w+\}", "", entry["en"]))


def test_a_term_parameter_is_rendered_in_the_recipients_language():
    assert words.render("order_delivery_code", "en", {"shop": Term("the_shop"), "delivery_code": "0427"})[1] == (
        "Your delivery code for the order from the shop is 0427: give it to the shop only when the order arrives.")
    # Only that the Term is rendered in Kannada: the case ending's form awaits the team's review of the Kannada text.
    assert words.render("order_delivery_code", "kn", {"shop": Term("the_shop"), "delivery_code": "0427"})[1] \
        .startswith("ಅಂಗಡಿ")


def test_user_text_in_a_parameter_is_never_formatted():
    title, body = words.render("reservation_rejected_reason", "kn", {"shop": "{shop}", "reason": "No {stock} today"})
    assert body == "No {stock} today" and title.startswith("{shop} ")


def stock_check_text(shop, stale, now, language="en"):
    """The stock check's title and text ({{1}} and {{2}}) as send_stock_checks renders them through notify()."""
    title, body = words.render(whatsapp.STOCK_CHECK_KIND, language, whatsapp.stock_check_params(shop, stale, now, language))
    return title, body or ""


# ---------------------------------------------------------------- English, word for word as before P7.11


def old_reservation_texts(customer, quantity, product, minutes, code, shop, hold, reason):
    """The English the reservation notifications had before P7.11, from the f-strings in services/reservations.py."""
    return {
        "reservation_new": (f"New reservation {code}",
                            f"{customer} wants {quantity} x {product}. Confirm within {minutes} min."),
        "reservation_confirmed": (f"{shop} confirmed your reservation",
                                  f"{quantity} x {product} is held for you for {hold} minutes. Code {code}."),
        "reservation_ready": ("Ready for pickup", f"{product} is packed at {shop}. Show code {code} at the counter."),
        "reservation_completed": ("Pickup complete",
                                  f"Thanks for shopping at {shop}. How was it? Leave a quick review."),
        "reservation_rejected": (f"{shop} could not hold your item",
                                 "The shop was unable to confirm this reservation. Try another shop nearby."),
        "reservation_rejected_reason": (f"{shop} could not hold your item", reason),
        "reservation_cancelled_by_customer": (f"Reservation {code} cancelled by customer",
                                              f"{quantity} x {product} is back in stock."),
        "reservation_cancelled_by_shop": (f"{shop} cancelled your reservation", "The shop cancelled this reservation."),
        "reservation_cancelled_by_shop_reason": (f"{shop} cancelled your reservation", reason),
        "reservation_request_expired": ("Reservation request timed out", f"{shop} did not respond in time."),
        "reservation_hold_expired": ("Your reservation expired", f"The hold on {product} at {shop} has ended."),
        "reservation_expired_shop": (f"Reservation {code} expired",
                                     f"{quantity} x {product} has been returned to stock."),
    }


def old_order_texts(code, summary, address, distance, total, shop, delivery_code, reason):
    """The English the order notifications had before P7.11 (CUSTOMER_MESSAGES and the f-strings in orders.py)."""
    old_customer = {
        "order_shop_confirmed": ("Order confirmed", "{shop} confirmed your order and set the items aside."),
        "order_preparing": ("Packing your order", "{shop} is preparing your order."),
        "order_out_for_delivery": ("Out for delivery",
                                   "Your order from {shop} is on the way. Keep cash ready: Rs {total:.0f}."),
        "order_delivered": ("Delivered", "Your order from {shop} was delivered. How did it go? Leave a quick review."),
        "order_delivery_failed": ("Delivery attempt failed",
                                  "{shop} could not complete the delivery. They may contact you."),
        "order_cancelled": ("Order cancelled", "Your order from {shop} was cancelled."),
    }
    out = {key: (title, body.format(shop=shop, total=total)) for key, (title, body) in old_customer.items()}
    title, body = out["order_shop_confirmed"]
    out["order_shop_confirmed"] = (title, body + f" Your delivery code is {delivery_code}: give it to the shop only "
                                                 "when the order arrives.")
    for key in ("order_cancelled", "order_delivery_failed"):
        out[f"{key}_reason"] = (out[key][0], out[key][1] + f" Reason: {reason}")
    out["order_cancelled_no_response"] = (out["order_cancelled"][0], out["order_cancelled"][1]
                                          + " Reason: The shop did not respond within an hour")
    out["order_new"] = (f"New delivery order {code}",
                        f"{summary} to {address[:60]} ({distance} km). COD Rs {total:.0f}.")
    out["order_delivery_code"] = ("Your delivery code", f"Your delivery code for the order from {shop} is "
                                                        f"{delivery_code}: give it to the shop only when the order "
                                                        "arrives.")
    out["order_cancelled_by_customer"] = (f"Order {code} cancelled by customer", None)
    return out


@pytest.mark.parametrize("values", [
    {"customer": "Asha", "quantity": 1, "product": "Tape", "minutes": 20, "code": "R-ABC123", "shop": "Shop A",
     "hold": 30, "reason": "Sold out"},
    {"customer": "Ravi K", "quantity": 12, "product": "Samsung 25W Charger", "minutes": 15, "code": "R-Z9Z9Z9",
     "shop": "Sri Ganesh Hardware & Paints", "hold": 240, "reason": "Closed for a festival {today}"},
])
def test_english_reservation_notifications_are_unchanged(values):
    old = old_reservation_texts(**values)
    for key, expected in old.items():
        # The confirmed notification gives the hold's minutes; the new request, the minutes to answer it.
        params = {**values, "minutes": values["hold"]} if key == "reservation_confirmed" else values
        assert words.render(key, "en", params) == expected, key
    assert set(old) == {k for k in MESSAGES if k.startswith("reservation_")}


@pytest.mark.parametrize("values", [
    {"code": "D-ABC123", "summary": "1 x Tape", "address": "12 MG Road", "distance": 1.2, "total": 120.0,
     "shop": "Shop A", "delivery_code": "0042", "reason": "Customer not home"},
    {"code": "D-Q1W2E3", "summary": "2 x Charger, 3 x Tape", "address": "Flat 4, " + "Long Street " * 10,
     "distance": 4.75, "total": 1499.5, "shop": "Sri Ganesh", "delivery_code": "9999", "reason": "Rain {heavy}"},
])
def test_english_order_notifications_are_unchanged(values):
    old = old_order_texts(**values)
    params = {**values, "distance_km": values["distance"], "address": values["address"][:60]}
    for key, expected in old.items():
        assert words.render(key, "en", params) == expected, key
    assert set(old) == {k for k in MESSAGES if k.startswith("order_")}


def test_english_bot_replies_are_unchanged():
    assert whatsapp.HELP_TEXT == (
        "To update your stock on NearShop, send one line per item with its name and count, for example:\n"
        "Thread seal tape 20\nSamsung charger 5\n\nWe show you the matches first, and change nothing until you "
        "reply Yes. Answer new reservations with the Confirm and Decline buttons.")
    pending = {"items": [{"name": "Tape", "quantity": 20, "before": 7}], "skipped": ["glue (no count)", "nails"],
               "queue": [{"line": "tape", "quantity": 3, "candidates": [{"name": "Tape"}, {"name": "Duct Tape"}]}]}
    assert whatsapp.preview_text(pending) == (
        "Matched so far:\n- Tape: 20 (now 7)\n\nNot matched, left out: glue (no count); nails\n\n"
        'Which item is "tape" (3)?\n1. Tape\n2. Duct Tape\nReply with its number, or 0 to leave this line out.')
    pending["queue"] = []
    assert whatsapp.preview_text(pending).endswith(
        "Update your stock to:\n- Tape: 20 (now 7)\n\nNot matched, left out: glue (no count); nails\n\n"
        "Reply Yes to save these counts, or No to cancel.")
    assert whatsapp.preview_text({"items": [], "skipped": [], "queue": []}) == (
        "Nothing matched your listings, so nothing will change. Check the names, or add the items in NearShop first.")
    en = {key: words.text(key, "en", PARAMS) for key in TEXTS}
    assert en["bot_not_confirmed"] == "Reservation R-7KQ2MX was not confirmed: Not enough stock."
    assert words.text("bot_not_declined", "en", {**PARAMS, "hint": en["bot_hint_insufficient_stock"]}) == (
        "Reservation R-7KQ2MX was not declined: Not enough stock. Send the new count to update your stock, or "
        "decline the request.")
    assert en["bot_confirmed"] == ("Confirmed reservation R-7KQ2MX: 2 x Astral PTFE Thread Seal Tape is held for the "
                                   "customer for 20 minutes. We have told the customer.")
    assert en["bot_declined"] == ("Declined reservation R-7KQ2MX for 2 x Astral PTFE Thread Seal Tape. We have told "
                                  "the customer.")
    assert en["bot_too_many_lines"] == "Please send at most 20 items in one message. Nothing was changed."
    assert en["bot_choose_number"] == "Please reply with a number from 1 to 3, or 0."
    now = utcnow()
    stale = [{"name": "Tape", "quantity": 3, "stock_updated_at": now - timedelta(days=4)}]
    assert stock_check_text({"name": None}, stale, now) == (
        "Stock check for your shop", "Are these counts still right? Tape: 3 (counted 4 days ago)")


# ---------------------------------------------------------------- fallback to English


def test_a_missing_kannada_message_falls_back_to_english(monkeypatch, caplog):
    monkeypatch.setitem(MESSAGES, "test_only_english", {"en": ("Hello {name}", "Body {name}")})
    assert words.render("test_only_english", "kn", {"name": "Asha"}) == ("Hello Asha", "Body Asha")
    monkeypatch.setitem(TEXTS, "test_only_english", {"en": "Reply {name}"})
    assert words.text("test_only_english", "kn", {"name": "Asha"}) == "Reply Asha"
    # A Kannada text that cannot be formatted (a parameter it names is not given) is sent in English, logged.
    monkeypatch.setitem(MESSAGES, "test_broken_kannada", {"en": ("Hi {name}", None), "kn": ("ನಮಸ್ಕಾರ {nmae}", None)})
    assert words.render("test_broken_kannada", "kn", {"name": "Asha"}) == ("Hi Asha", None)
    assert "sent in English" in caplog.text


def test_an_unknown_or_missing_language_reads_as_english():
    assert words.language_of(None) == words.language_of("te") == words.language_of(3) == "en"
    assert words.language_of("kn") == "kn"
    assert words.render("reservation_ready", "te", PARAMS) == words.render("reservation_ready", "en", PARAMS)
    assert whatsapp.template_language("te") == whatsapp.template_language(None) == settings.whatsapp_template_language
    assert whatsapp.template_language("kn") == "kn"


# ---------------------------------------------------------------- the recipient's language


@pytest.fixture
def kannada_customer(ids, monkeypatch):
    """The seeded customer reading Kannada, with a verified phone and the WhatsApp consent; WhatsApp on (fake
    client). Everything put back afterwards."""
    monkeypatch.setattr(settings, "whatsapp_notifications", True)
    db = Database()
    user = db.users.find_one({"email": "cust@example.com"})
    saved = {"phone": user.phone, "phone_verified_at": user.phone_verified_at}
    before = {r["_id"] for r in db.mongo["consents"].find({"user_id": user.id}, {"_id": 1})}
    db.users.update_one({"_id": user.id}, {"$set": {"phone": CUSTOMER_PHONE, "phone_verified_at": utcnow(),
                                                     "language": "kn"}})
    set_consent(db, user.id, "whatsapp", True)
    db.mongo[whatsapp.OUTBOX].delete_many({})
    whatsapp.FAKE.reset()
    yield user
    db.mongo[whatsapp.OUTBOX].delete_many({})
    whatsapp.FAKE.reset()
    db.mongo["consents"].delete_many({"user_id": user.id, "_id": {"$nin": list(before)}})
    db.users.update_one({"_id": user.id}, {"$set": {**saved, "language": "en"}})


def test_the_profile_sets_the_language_me_returns_it_and_bad_values_are_refused(customer, ids):
    try:
        assert customer.get("/api/auth/me").json()["language"] == "en"
        res = customer.patch("/api/auth/me", json={"language": "kn"}, headers=HEADERS)
        assert res.status_code == 200 and res.json()["language"] == "kn"
        assert customer.get("/api/auth/me").json()["language"] == "kn"
        for bad in ("fr", None, "KN", ""):
            assert customer.patch("/api/auth/me", json={"language": bad}, headers=HEADERS).status_code == 422
        assert customer.patch("/api/auth/me", json={"name": "Customer"}, headers=HEADERS).json()["language"] == "kn"
    finally:
        customer.patch("/api/auth/me", json={"language": "en"}, headers=HEADERS)


def test_the_users_validator_refuses_an_unknown_language(ids):
    db = Database()
    user = db.users.find_one({"email": "cust@example.com"})
    with pytest.raises(WriteError):
        db.users.update_one({"_id": user.id}, {"$set": {"language": "fr"}})
    db.users.update_one({"_id": user.id}, {"$set": {"language": "kn"}})
    db.users.update_one({"_id": user.id}, {"$set": {"language": "en"}})


def test_a_kannada_customer_gets_kannada_for_confirm_ready_and_expire(kannada_customer, customer, owner_a, ids):
    db = Database()
    since = utcnow() - timedelta(seconds=1)
    r = customer.post("/api/reservations", json={"product_id": ids["tape"], "quantity": 1}, headers=HEADERS).json()
    try:
        # The shop's owner reads English: the new-reservation notification is English, word for word.
        [new] = db.notifications.find({"user_id": db.shops.get(ids["shop_a"]).owner_id, "kind": "reservation_new",
                                       "created_at": {"$gte": since}})
        assert new.title == f"New reservation {r['code']}" and not KANNADA.search(new.body)
        assert owner_a.post(f"/api/owner/reservations/{r['id']}/confirm", json={}, headers=HEADERS).status_code == 200
        assert owner_a.post(f"/api/owner/reservations/{r['id']}/ready", json={}, headers=HEADERS).status_code == 200
        db.reservations.update_one({"_id": r["id"]}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})
        reservations.expire_due(db)
        notes = {n.kind: n for n in db.notifications.find({"user_id": kannada_customer.id,
                                                           "created_at": {"$gte": since}})}
        assert set(notes) >= {"reservation_confirmed", "reservation_ready", "reservation_expired"}
        shop, product = db.shops.get(ids["shop_a"]), db.products.get(ids["tape"])
        res = db.reservations.get(r["id"])
        assert (notes["reservation_confirmed"].title, notes["reservation_confirmed"].body) == words.render(
            "reservation_confirmed", "kn", {"shop": shop.name, "quantity": 1, "product": product.name,
                                            "minutes": res.hold_minutes, "code": r["code"]})
        assert (notes["reservation_ready"].title, notes["reservation_ready"].body) == words.render(
            "reservation_ready", "kn", {"product": product.name, "shop": shop.name, "code": r["code"]})
        assert (notes["reservation_expired"].title, notes["reservation_expired"].body) == words.render(
            "reservation_hold_expired", "kn", {"product": product.name, "shop": shop.name})
        # WhatsApp: the same Kannada text, in the Kannada templates.
        rows = list(db.mongo[whatsapp.OUTBOX].find({"user_id": kannada_customer.id}, sort=[("_id", 1)]))
        assert [row["kind"] for row in rows] == ["reservation_confirmed", "reservation_ready", "reservation_expired"]
        assert all(row["language"] == "kn" and KANNADA.search(row["params"][0]) for row in rows)
        assert rows[0]["params"][0] == notes["reservation_confirmed"].title
        assert whatsapp.send_due(db, client=whatsapp.FAKE)["sent"] >= 3
        sent = [m for m in whatsapp.FAKE.sent if m["phone"] == CUSTOMER_PHONE]
        assert {m["template"] for m in sent} == {"nearshop_reservation_confirmed", "nearshop_reservation_ready",
                                                 "nearshop_reservation_closed"}
        assert all(m["language"] == "kn" for m in sent)
        owner_rows = list(db.mongo[whatsapp.OUTBOX].find({"user_id": {"$ne": kannada_customer.id}}))
        assert all(row["language"] == settings.whatsapp_template_language for row in owner_rows)
    finally:
        db.reservations.update_one({"_id": r["id"]}, {"$set": {"status": "CANCELLED", "stock_held": False}})


def test_a_kannada_customer_gets_kannada_order_updates_with_an_english_reason(kannada_customer, ids):
    db = Database()
    shop = db.shops.get(ids["shop_a"])
    db.transaction(lambda: notify(db, kannada_customer.id, "order_cancelled", link="/account/orders/1",
                                  message="order_cancelled_reason", params={"shop": shop.name, "reason": "Rain"}))
    note = db.notifications.find({"user_id": kannada_customer.id, "kind": "order_cancelled"},
                                 sort=[("_id", -1)])[0]
    assert note.title == "ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ" and note.body.endswith("ಕಾರಣ: Rain")
    db.notifications.delete_many({"_id": note.id})


def test_the_bot_answers_a_kannada_owner_in_kannada(ids):
    db = Database()
    owner = db.users.find_one({"email": "a@example.com"}).model_copy(update={"language": "kn"})
    shop = db.shops.get(ids["shop_a"])
    outcome, reply = whatsapp.answer_text(db, owner, shop, "hello there", utcnow())
    assert outcome == "help" and reply == TEXTS["bot_help"]["kn"]
    outcome, reply = whatsapp.answer_button(db, owner, shop, "nonsense")
    assert outcome == "bad_button" and reply == TEXTS["bot_bad_button"]["kn"]
    english = owner.model_copy(update={"language": "en"})
    assert whatsapp.answer_text(db, english, shop, "hello there", utcnow())[1] == whatsapp.HELP_TEXT


def test_the_stock_check_is_in_the_owners_language():
    now = utcnow()
    stale = [{"name": f"Listing {n} " + "with a very long descriptive name " * 3, "quantity": 100 + n,
              "stock_updated_at": now - timedelta(days=10 + n)} for n in range(whatsapp.STOCK_CHECK_ITEMS)]
    title, body = stock_check_text({"name": "Sri Ganesh Hardware"}, stale, now, "kn")
    assert title == "Sri Ganesh Hardware ಸ್ಟಾಕ್ ಪರಿಶೀಲನೆ"
    assert len(body) <= whatsapp.PARAM_MAX and whatsapp._one_line(body) == body
    assert all(f"Listing {n}" in body and f": {100 + n} ({10 + n} ದಿನಗಳ ಹಿಂದೆ ಎಣಿಸಲಾಗಿದೆ)" in body
               for n in range(whatsapp.STOCK_CHECK_ITEMS))
