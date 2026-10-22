"""Notifications. notify() is the single seam for every channel: it writes the in-app notification and, in the same
transaction, an outbox row for each other channel the recipient opted into (P4.3). The outbox channels are in
OUTBOX_CHANNELS; today WhatsApp (app/services/whatsapp.py), whose sender job delivers the rows outside the
transaction, so a slow or failing send never blocks or fails the reservation or order that caused it.

Callers pass a message key and its parameters (P7.11): notify() renders the title and body in the recipient's
language (User.language) from the catalogue in app/services/notification_text.py, and the WhatsApp row goes out in
the template language of the same name. A caller may still pass a finished title and body instead; those are stored
as they are (the admin, grievance and privacy notices, which are not in the catalogue yet)."""
from collections.abc import Callable, Mapping
from typing import Any

from app.core.database import Database
from app.models import Notification
from app.services import notification_text, whatsapp

# Each takes the database handle (in the caller's transaction) and the in-app notification just written, and writes
# that channel's outbox row when the recipient opted in.
OUTBOX_CHANNELS: tuple[Callable[[Database, Notification], object], ...] = (whatsapp.enqueue,)


def notify(db: Database, user_id: int, kind: str, title: str | None = None, body: str | None = None,
           link: str | None = None, *, message: str | None = None, params: Mapping[str, Any] | None = None
           ) -> Notification:
    """Notify a user and return the in-app notification written. message: a key of notification_text.MESSAGES,
    rendered with params in the recipient's language; otherwise title (and body) are the finished text."""
    if message is not None:
        title, body = notification_text.render(message, notification_text.recipient_language(db, user_id), params)
    if not title:
        raise ValueError("notify needs a message key or a title")
    note = db.notifications.insert(Notification(user_id=user_id, kind=kind, title=title, body=body, link=link))
    for enqueue in OUTBOX_CHANNELS:
        enqueue(db, note)
    return note
