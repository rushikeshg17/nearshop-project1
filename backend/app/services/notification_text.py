"""The words of every notification and WhatsApp bot reply, in each language a user can choose (P7.11).

A caller of notify() (app/services/notifications.py) passes a message key and its parameters, never finished text.
notify() reads the recipient's language (User.language, en or kn) and renders the title and body here, for the in-app
row; the WhatsApp outbox row (app/services/whatsapp.py) carries the same rendered text as its two template variables
and is sent in the template language of the same name. The bot's replies to shops (P4.4) come from TEXTS, in the
owner's language.

MESSAGES holds each notification's title and body; TEXTS holds the bot's replies and the fragments notifications are
built from (one listing in the stock check, a word for "the shop" when the shop has no name). Each entry has an en
text, which is the source and always present, and a text per other language. A language missing from an entry, or a
text that cannot be formatted with the parameters given, falls back to English, so a gap in a translation never
loses a message.

Parameters are formatted with str.format: names in braces, with a format spec where a number needs one ({total:.0f}).
A Term parameter is a word from TEXTS rendered in the recipient's language. What a person typed (a reason, a
listing's name, a shop's name) is passed as it is and never translated.

The Kannada text is an implementer's draft, not yet text from the team: a native Kannada speaker on the team must
review and replace it before Kannada is offered to users. One known pattern to fix in that review: case endings are
written as separate words after a name ({shop} ನಿಂದ, {shop} ಗೆ, {shop} ನಲ್ಲಿ), which reads acceptably after a shop's own
name but not after the fallback word (ಅಂಗಡಿ ನಿಂದ rather than ಅಂಗಡಿಯಿಂದ). The English is the text NearShop has always
sent, word for word.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.models.core import DEFAULT_LANGUAGE, LANGUAGES

log = logging.getLogger("nearshop.notifications")


@dataclass(frozen=True)
class Term:
    """A parameter that is itself a word from TEXTS, rendered in the recipient's language."""

    key: str


# key -> language -> (title, body). body None: the notification has a title only.
MESSAGES: dict[str, dict[str, tuple[str, str | None]]] = {
    # ------------------------------------------------------------ reservations (app/services/reservations.py)
    "reservation_new": {  # to the shop's owner
        "en": ("New reservation {code}",
               "{customer} wants {quantity} x {product}. Confirm within {minutes} min."),
        "kn": ("ಹೊಸ ಕಾಯ್ದಿರಿಸುವಿಕೆ {code}",
               "{customer} ಅವರಿಗೆ {quantity} x {product} ಬೇಕು. {minutes} ನಿಮಿಷಗಳಲ್ಲಿ ದೃಢೀಕರಿಸಿ."),
    },
    "reservation_confirmed": {
        "en": ("{shop} confirmed your reservation",
               "{quantity} x {product} is held for you for {minutes} minutes. Code {code}."),
        "kn": ("{shop} ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆಯನ್ನು ದೃಢೀಕರಿಸಿದೆ",
               "{quantity} x {product} ಅನ್ನು ನಿಮಗಾಗಿ {minutes} ನಿಮಿಷಗಳ ಕಾಲ ಹಿಡಿದಿಡಲಾಗಿದೆ. ಕೋಡ್ {code}."),
    },
    "reservation_ready": {
        "en": ("Ready for pickup", "{product} is packed at {shop}. Show code {code} at the counter."),
        "kn": ("ಪಡೆಯಲು ಸಿದ್ಧವಾಗಿದೆ", "{product} ಅನ್ನು {shop} ನಲ್ಲಿ ಪ್ಯಾಕ್ ಮಾಡಲಾಗಿದೆ. ಕೌಂಟರ್‌ನಲ್ಲಿ ಕೋಡ್ {code} ತೋರಿಸಿ."),
    },
    "reservation_completed": {
        "en": ("Pickup complete", "Thanks for shopping at {shop}. How was it? Leave a quick review."),
        "kn": ("ಪಡೆಯುವಿಕೆ ಪೂರ್ಣಗೊಂಡಿದೆ",
               "{shop} ನಲ್ಲಿ ಖರೀದಿಸಿದ್ದಕ್ಕೆ ಧನ್ಯವಾದಗಳು. ಹೇಗಿತ್ತು? ಒಂದು ಚಿಕ್ಕ ವಿಮರ್ಶೆ ಬರೆಯಿರಿ."),
    },
    "reservation_rejected": {
        "en": ("{shop} could not hold your item",
               "The shop was unable to confirm this reservation. Try another shop nearby."),
        "kn": ("{shop} ನಿಮ್ಮ ವಸ್ತುವನ್ನು ಹಿಡಿದಿಡಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ",
               "ಅಂಗಡಿಗೆ ಈ ಕಾಯ್ದಿರಿಸುವಿಕೆಯನ್ನು ದೃಢೀಕರಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ಹತ್ತಿರದ ಇನ್ನೊಂದು ಅಂಗಡಿಯನ್ನು ಪ್ರಯತ್ನಿಸಿ."),
    },
    "reservation_rejected_reason": {  # the owner's own words as the body
        "en": ("{shop} could not hold your item", "{reason}"),
        "kn": ("{shop} ನಿಮ್ಮ ವಸ್ತುವನ್ನು ಹಿಡಿದಿಡಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ", "{reason}"),
    },
    "reservation_cancelled_by_customer": {  # to the shop's owner
        "en": ("Reservation {code} cancelled by customer", "{quantity} x {product} is back in stock."),
        "kn": ("ಕಾಯ್ದಿರಿಸುವಿಕೆ {code} ಅನ್ನು ಗ್ರಾಹಕರು ರದ್ದುಗೊಳಿಸಿದ್ದಾರೆ",
               "{quantity} x {product} ಮತ್ತೆ ಸ್ಟಾಕ್‌ನಲ್ಲಿದೆ."),
    },
    "reservation_cancelled_by_shop": {
        "en": ("{shop} cancelled your reservation", "The shop cancelled this reservation."),
        "kn": ("{shop} ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆಯನ್ನು ರದ್ದುಗೊಳಿಸಿದೆ", "ಅಂಗಡಿ ಈ ಕಾಯ್ದಿರಿಸುವಿಕೆಯನ್ನು ರದ್ದುಗೊಳಿಸಿದೆ."),
    },
    "reservation_cancelled_by_shop_reason": {
        "en": ("{shop} cancelled your reservation", "{reason}"),
        "kn": ("{shop} ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆಯನ್ನು ರದ್ದುಗೊಳಿಸಿದೆ", "{reason}"),
    },
    "reservation_request_expired": {  # a request the shop never answered
        "en": ("Reservation request timed out", "{shop} did not respond in time."),
        "kn": ("ಕಾಯ್ದಿರಿಸುವಿಕೆ ವಿನಂತಿಯ ಸಮಯ ಮುಗಿದಿದೆ", "{shop} ಸಮಯಕ್ಕೆ ಸರಿಯಾಗಿ ಪ್ರತಿಕ್ರಿಯಿಸಲಿಲ್ಲ."),
    },
    "reservation_hold_expired": {  # a confirmed hold nobody collected
        "en": ("Your reservation expired", "The hold on {product} at {shop} has ended."),
        "kn": ("ನಿಮ್ಮ ಕಾಯ್ದಿರಿಸುವಿಕೆಯ ಅವಧಿ ಮುಗಿದಿದೆ", "{shop} ನಲ್ಲಿ {product} ಹಿಡಿದಿಡುವ ಅವಧಿ ಮುಗಿದಿದೆ."),
    },
    "reservation_expired_shop": {  # to the shop's owner
        "en": ("Reservation {code} expired", "{quantity} x {product} has been returned to stock."),
        "kn": ("ಕಾಯ್ದಿರಿಸುವಿಕೆ {code} ಅವಧಿ ಮುಗಿದಿದೆ", "{quantity} x {product} ಅನ್ನು ಮತ್ತೆ ಸ್ಟಾಕ್‌ಗೆ ಸೇರಿಸಲಾಗಿದೆ."),
    },
    # ------------------------------------------------------------ delivery orders (app/services/orders.py)
    "order_new": {  # to the shop's owner
        "en": ("New delivery order {code}", "{summary} to {address} ({distance_km} km). COD Rs {total:.0f}."),
        "kn": ("ಹೊಸ ಡೆಲಿವರಿ ಆರ್ಡರ್ {code}",
               "{summary}, ವಿಳಾಸ: {address} ({distance_km} ಕಿ.ಮೀ). ಡೆಲಿವರಿ ವೇಳೆ ಪಾವತಿ ರೂ {total:.0f}."),
    },
    "order_delivery_code": {
        "en": ("Your delivery code",
               "Your delivery code for the order from {shop} is {delivery_code}: give it to the shop only when the "
               "order arrives."),
        "kn": ("ನಿಮ್ಮ ಡೆಲಿವರಿ ಕೋಡ್",
               "{shop} ನಿಂದ ಬರುವ ಆರ್ಡರ್‌ನ ನಿಮ್ಮ ಡೆಲಿವರಿ ಕೋಡ್ {delivery_code}: ಆರ್ಡರ್ ತಲುಪಿದಾಗ ಮಾತ್ರ ಅದನ್ನು "
               "ಅಂಗಡಿಗೆ ನೀಡಿ."),
    },
    "order_shop_confirmed": {  # the customer's own notification: the delivery code is theirs to give
        "en": ("Order confirmed",
               "{shop} confirmed your order and set the items aside. Your delivery code is {delivery_code}: give it "
               "to the shop only when the order arrives."),
        "kn": ("ಆರ್ಡರ್ ದೃಢೀಕರಿಸಲಾಗಿದೆ",
               "{shop} ನಿಮ್ಮ ಆರ್ಡರ್ ಅನ್ನು ದೃಢೀಕರಿಸಿ ವಸ್ತುಗಳನ್ನು ತೆಗೆದಿರಿಸಿದೆ. ನಿಮ್ಮ ಡೆಲಿವರಿ ಕೋಡ್ {delivery_code}: "
               "ಆರ್ಡರ್ ತಲುಪಿದಾಗ ಮಾತ್ರ ಅದನ್ನು ಅಂಗಡಿಗೆ ನೀಡಿ."),
    },
    "order_preparing": {
        "en": ("Packing your order", "{shop} is preparing your order."),
        "kn": ("ನಿಮ್ಮ ಆರ್ಡರ್ ಪ್ಯಾಕ್ ಆಗುತ್ತಿದೆ", "{shop} ನಿಮ್ಮ ಆರ್ಡರ್ ಅನ್ನು ಸಿದ್ಧಪಡಿಸುತ್ತಿದೆ."),
    },
    "order_out_for_delivery": {
        "en": ("Out for delivery", "Your order from {shop} is on the way. Keep cash ready: Rs {total:.0f}."),
        "kn": ("ಡೆಲಿವರಿಗೆ ಹೊರಟಿದೆ", "{shop} ನಿಂದ ನಿಮ್ಮ ಆರ್ಡರ್ ದಾರಿಯಲ್ಲಿದೆ. ಹಣ ಸಿದ್ಧವಾಗಿಡಿ: ರೂ {total:.0f}."),
    },
    "order_delivered": {
        "en": ("Delivered", "Your order from {shop} was delivered. How did it go? Leave a quick review."),
        "kn": ("ತಲುಪಿಸಲಾಗಿದೆ", "{shop} ನಿಂದ ನಿಮ್ಮ ಆರ್ಡರ್ ತಲುಪಿಸಲಾಗಿದೆ. ಹೇಗಿತ್ತು? ಒಂದು ಚಿಕ್ಕ ವಿಮರ್ಶೆ ಬರೆಯಿರಿ."),
    },
    "order_delivery_failed": {
        "en": ("Delivery attempt failed", "{shop} could not complete the delivery. They may contact you."),
        "kn": ("ಡೆಲಿವರಿ ಪ್ರಯತ್ನ ವಿಫಲವಾಗಿದೆ",
               "{shop} ಗೆ ಡೆಲಿವರಿ ಪೂರ್ಣಗೊಳಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ಅವರು ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸಬಹುದು."),
    },
    "order_delivery_failed_reason": {
        "en": ("Delivery attempt failed",
               "{shop} could not complete the delivery. They may contact you. Reason: {reason}"),
        "kn": ("ಡೆಲಿವರಿ ಪ್ರಯತ್ನ ವಿಫಲವಾಗಿದೆ",
               "{shop} ಗೆ ಡೆಲಿವರಿ ಪೂರ್ಣಗೊಳಿಸಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ಅವರು ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸಬಹುದು. ಕಾರಣ: {reason}"),
    },
    "order_cancelled": {
        "en": ("Order cancelled", "Your order from {shop} was cancelled."),
        "kn": ("ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ", "{shop} ನಿಂದ ನಿಮ್ಮ ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ."),
    },
    "order_cancelled_reason": {
        "en": ("Order cancelled", "Your order from {shop} was cancelled. Reason: {reason}"),
        "kn": ("ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ", "{shop} ನಿಂದ ನಿಮ್ಮ ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ. ಕಾರಣ: {reason}"),
    },
    "order_cancelled_no_response": {  # cancelled by the system: the shop did not answer within the hour
        "en": ("Order cancelled",
               "Your order from {shop} was cancelled. Reason: The shop did not respond within an hour"),
        "kn": ("ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ",
               "{shop} ನಿಂದ ನಿಮ್ಮ ಆರ್ಡರ್ ರದ್ದಾಗಿದೆ. ಕಾರಣ: ಅಂಗಡಿ ಒಂದು ಗಂಟೆಯೊಳಗೆ ಪ್ರತಿಕ್ರಿಯಿಸಲಿಲ್ಲ"),
    },
    "order_cancelled_by_customer": {  # to the shop's owner
        "en": ("Order {code} cancelled by customer", None),
        "kn": ("ಆರ್ಡರ್ {code} ಅನ್ನು ಗ್ರಾಹಕರು ರದ್ದುಗೊಳಿಸಿದ್ದಾರೆ", None),
    },
    # ------------------------------------------------------------ the daily stock check (app/services/whatsapp.py)
    "stock_check": {  # items: the stock_check_item fragments, joined by "; "
        "en": ("Stock check for {shop}", "Are these counts still right? {items}"),
        "kn": ("{shop} ಸ್ಟಾಕ್ ಪರಿಶೀಲನೆ", "ಈ ಸಂಖ್ಯೆಗಳು ಇನ್ನೂ ಸರಿಯಾಗಿವೆಯೇ? {items}"),
    },
}

# key -> language -> text: the WhatsApp bot's replies to shops (P4.4) and the fragments messages are built from.
TEXTS: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------ words used as Term parameters
    "the_shop": {"en": "the shop", "kn": "ಅಂಗಡಿ"},
    "your_shop": {"en": "your shop", "kn": "ನಿಮ್ಮ ಅಂಗಡಿ"},
    "the_item": {"en": "the item", "kn": "ವಸ್ತು"},
    # ------------------------------------------------------------ the stock check's listings
    "stock_check_item": {"en": "{name}: {quantity} (counted {days} days ago)",
                         "kn": "{name}: {quantity} ({days} ದಿನಗಳ ಹಿಂದೆ ಎಣಿಸಲಾಗಿದೆ)"},
    # ------------------------------------------------------------ the bot's replies
    "bot_help": {
        "en": "To update your stock on NearShop, send one line per item with its name and count, for example:\n"
              "Thread seal tape 20\nSamsung charger 5\n\nWe show you the matches first, and change nothing until you "
              "reply Yes. Answer new reservations with the Confirm and Decline buttons.",
        "kn": "NearShop ನಲ್ಲಿ ನಿಮ್ಮ ಸ್ಟಾಕ್ ನವೀಕರಿಸಲು, ಪ್ರತಿ ವಸ್ತುವಿನ ಹೆಸರು ಮತ್ತು ಸಂಖ್ಯೆಯನ್ನು ಸಾಲಿಗೊಂದರಂತೆ "
              "ಕಳುಹಿಸಿ, ಉದಾಹರಣೆಗೆ:\nThread seal tape 20\nSamsung charger 5\n\nಮೊದಲು ನಾವು ಹೊಂದಾಣಿಕೆಗಳನ್ನು "
              "ತೋರಿಸುತ್ತೇವೆ. ನೀವು Yes ಎಂದು ಉತ್ತರಿಸುವವರೆಗೆ ಏನೂ ಬದಲಾಗುವುದಿಲ್ಲ. ಹೊಸ ಕಾಯ್ದಿರಿಸುವಿಕೆಗಳಿಗೆ "
              "ದೃಢೀಕರಿಸಿ ಮತ್ತು ನಿರಾಕರಿಸಿ ಬಟನ್‌ಗಳ ಮೂಲಕ ಉತ್ತರಿಸಿ.",
    },
    "bot_bad_button": {
        "en": "Sorry, we could not read that button. Please answer the request in NearShop.",
        "kn": "ಕ್ಷಮಿಸಿ, ಆ ಬಟನ್ ಅನ್ನು ಓದಲು ಸಾಧ್ಯವಾಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು NearShop ನಲ್ಲಿ ವಿನಂತಿಗೆ ಉತ್ತರಿಸಿ.",
    },
    "bot_reservation_not_found": {
        "en": "We could not find that reservation in your shop. Please check it in NearShop.",
        "kn": "ಆ ಕಾಯ್ದಿರಿಸುವಿಕೆ ನಿಮ್ಮ ಅಂಗಡಿಯಲ್ಲಿ ಸಿಗಲಿಲ್ಲ. ದಯವಿಟ್ಟು NearShop ನಲ್ಲಿ ಪರಿಶೀಲಿಸಿ.",
    },
    # error: the refusal's own message, without its last full stop; hint: bot_hint_insufficient_stock or "".
    "bot_not_confirmed": {"en": "Reservation {code} was not confirmed: {error}.{hint}",
                          "kn": "ಕಾಯ್ದಿರಿಸುವಿಕೆ {code} ದೃಢೀಕರಿಸಲಾಗಿಲ್ಲ: {error}.{hint}"},
    "bot_not_declined": {"en": "Reservation {code} was not declined: {error}.{hint}",
                         "kn": "ಕಾಯ್ದಿರಿಸುವಿಕೆ {code} ನಿರಾಕರಿಸಲಾಗಿಲ್ಲ: {error}.{hint}"},
    "bot_hint_insufficient_stock": {
        "en": " Send the new count to update your stock, or decline the request.",
        "kn": " ಸ್ಟಾಕ್ ನವೀಕರಿಸಲು ಹೊಸ ಸಂಖ್ಯೆಯನ್ನು ಕಳುಹಿಸಿ, ಅಥವಾ ವಿನಂತಿಯನ್ನು ನಿರಾಕರಿಸಿ.",
    },
    "bot_confirmed": {
        "en": "Confirmed reservation {code}: {quantity} x {product} is held for the customer for {minutes} minutes. "
              "We have told the customer.",
        "kn": "ಕಾಯ್ದಿರಿಸುವಿಕೆ {code} ದೃಢೀಕರಿಸಲಾಗಿದೆ: {quantity} x {product} ಅನ್ನು ಗ್ರಾಹಕರಿಗಾಗಿ {minutes} ನಿಮಿಷಗಳ "
              "ಕಾಲ ಹಿಡಿದಿಡಲಾಗಿದೆ. ನಾವು ಗ್ರಾಹಕರಿಗೆ ತಿಳಿಸಿದ್ದೇವೆ.",
    },
    "bot_declined": {
        "en": "Declined reservation {code} for {quantity} x {product}. We have told the customer.",
        "kn": "{quantity} x {product} ಗಾಗಿ ಕಾಯ್ದಿರಿಸುವಿಕೆ {code} ನಿರಾಕರಿಸಲಾಗಿದೆ. ನಾವು ಗ್ರಾಹಕರಿಗೆ ತಿಳಿಸಿದ್ದೇವೆ.",
    },
    # The stock-update preview (whatsapp.preview_text), built from these parts.
    "preview_matched_so_far": {"en": "Matched so far:", "kn": "ಇಲ್ಲಿಯವರೆಗೆ ಹೊಂದಿಕೆಯಾದವು:"},
    "preview_update_to": {"en": "Update your stock to:", "kn": "ನಿಮ್ಮ ಸ್ಟಾಕ್ ಅನ್ನು ಹೀಗೆ ನವೀಕರಿಸಿ:"},
    "preview_item": {"en": "- {name}: {quantity} (now {before})", "kn": "- {name}: {quantity} (ಈಗ {before})"},
    "preview_skipped": {"en": "Not matched, left out: {lines}", "kn": "ಹೊಂದಿಕೆಯಾಗಿಲ್ಲ, ಬಿಡಲಾಗಿದೆ: {lines}"},
    "preview_no_count": {"en": "{line} (no count)", "kn": "{line} (ಸಂಖ್ಯೆ ಇಲ್ಲ)"},
    "preview_which": {
        "en": 'Which item is "{line}" ({quantity})?\n{options}\nReply with its number, or 0 to leave this line out.',
        "kn": '"{line}" ({quantity}) ಯಾವ ವಸ್ತು?\n{options}\nಅದರ ಸಂಖ್ಯೆಯೊಂದಿಗೆ ಉತ್ತರಿಸಿ, ಅಥವಾ ಈ ಸಾಲನ್ನು ಬಿಡಲು 0 '
              'ಕಳುಹಿಸಿ.',
    },
    "preview_confirm": {"en": "Reply Yes to save these counts, or No to cancel.",
                        "kn": "ಈ ಸಂಖ್ಯೆಗಳನ್ನು ಉಳಿಸಲು Yes ಎಂದು, ರದ್ದುಗೊಳಿಸಲು No ಎಂದು ಉತ್ತರಿಸಿ."},
    "preview_nothing": {
        "en": "Nothing matched your listings, so nothing will change. Check the names, or add the items in NearShop "
              "first.",
        "kn": "ನಿಮ್ಮ ಪಟ್ಟಿಯಲ್ಲಿರುವ ಯಾವುದಕ್ಕೂ ಹೊಂದಿಕೆಯಾಗಲಿಲ್ಲ, ಆದ್ದರಿಂದ ಏನೂ ಬದಲಾಗುವುದಿಲ್ಲ. ಹೆಸರುಗಳನ್ನು "
              "ಪರಿಶೀಲಿಸಿ, ಅಥವಾ ಮೊದಲು ವಸ್ತುಗಳನ್ನು NearShop ನಲ್ಲಿ ಸೇರಿಸಿ.",
    },
    "bot_choose_first": {"en": "Please choose the item first.", "kn": "ದಯವಿಟ್ಟು ಮೊದಲು ವಸ್ತುವನ್ನು ಆಯ್ಕೆಮಾಡಿ."},
    "bot_choose_number": {"en": "Please reply with a number from 1 to {count}, or 0.",
                          "kn": "ದಯವಿಟ್ಟು 1 ರಿಂದ {count} ವರೆಗಿನ ಸಂಖ್ಯೆ ಅಥವಾ 0 ಎಂದು ಉತ್ತರಿಸಿ."},
    "bot_nothing_to_confirm": {"en": "There is no stock update waiting for your Yes.",
                               "kn": "ನಿಮ್ಮ Yes ಗಾಗಿ ಯಾವುದೇ ಸ್ಟಾಕ್ ನವೀಕರಣ ಕಾಯುತ್ತಿಲ್ಲ."},
    "bot_cancelled": {"en": "Cancelled. Your stock was not changed.",
                      "kn": "ರದ್ದುಗೊಳಿಸಲಾಗಿದೆ. ನಿಮ್ಮ ಸ್ಟಾಕ್ ಬದಲಾಗಿಲ್ಲ."},
    "bot_too_many_lines": {"en": "Please send at most {max} items in one message. Nothing was changed.",
                           "kn": "ದಯವಿಟ್ಟು ಒಂದು ಸಂದೇಶದಲ್ಲಿ ಗರಿಷ್ಠ {max} ವಸ್ತುಗಳನ್ನು ಕಳುಹಿಸಿ. ಏನೂ ಬದಲಾಗಿಲ್ಲ."},
    "bot_items_gone": {"en": "Those items are no longer in your shop, so nothing was changed.",
                       "kn": "ಆ ವಸ್ತುಗಳು ಈಗ ನಿಮ್ಮ ಅಂಗಡಿಯಲ್ಲಿ ಇಲ್ಲ, ಆದ್ದರಿಂದ ಏನೂ ಬದಲಾಗಿಲ್ಲ."},
    "bot_saved": {"en": "Saved. Your stock is now:", "kn": "ಉಳಿಸಲಾಗಿದೆ. ನಿಮ್ಮ ಸ್ಟಾಕ್ ಈಗ:"},
    "bot_saved_item": {"en": "- {name}: {quantity}", "kn": "- {name}: {quantity}"},
}


def language_of(value: Any) -> str:
    """A supported language code for a stored or requested value: anything else (None, a language not offered yet)
    reads as the default, English."""
    return value if isinstance(value, str) and value in LANGUAGES else DEFAULT_LANGUAGE


def recipient_language(db: Any, user_id: int) -> str:
    """The language a user reads notifications in, read in db's transaction (the caller's)."""
    doc = db.users.raw.find_one({"_id": user_id}, {"language": 1}, session=db.session)
    return language_of(doc.get("language") if doc else None)


def _params(params: Mapping[str, Any] | None, language: str) -> dict[str, Any]:
    return {k: (text(v.key, language) if isinstance(v, Term) else v) for k, v in (params or {}).items()}


def _format(template: str | None, params: dict[str, Any]) -> str | None:
    return None if template is None else template.format_map(params)


def render(key: str, language: str, params: Mapping[str, Any] | None = None) -> tuple[str, str | None]:
    """A notification's (title, body) in the language, with its parameters. A language the entry lacks, or a text
    that cannot be formatted with these parameters, falls back to English. An unknown key raises KeyError."""
    entry = MESSAGES[key]
    language = language_of(language)
    if language != DEFAULT_LANGUAGE and language in entry:
        try:
            title, body = entry[language]
            values = _params(params, language)
            return _format(title, values) or "", _format(body, values)
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("notification %s in %s could not be formatted (%s); sent in English", key, language,
                        type(exc).__name__)
    title, body = entry[DEFAULT_LANGUAGE]
    values = _params(params, DEFAULT_LANGUAGE)
    return _format(title, values) or "", _format(body, values)


def text(key: str, language: str, params: Mapping[str, Any] | None = None) -> str:
    """One text from TEXTS in the language, with its parameters, falling back to English like render."""
    entry = TEXTS[key]
    language = language_of(language)
    if language != DEFAULT_LANGUAGE and language in entry:
        try:
            return entry[language].format_map(_params(params, language))
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("text %s in %s could not be formatted (%s); sent in English", key, language,
                        type(exc).__name__)
    return entry[DEFAULT_LANGUAGE].format_map(_params(params, DEFAULT_LANGUAGE))
