"""Notifications. notify() is the single seam for every channel: it writes the in-app notification and, in the same
transaction, an outbox row for each other channel the recipient opted into (P4.3). The outbox channels are in
OUTBOX_CHANNELS; today WhatsApp (app/services/whatsapp.py), whose sender job delivers the rows outside the
transaction, so a slow or failing send never blocks or fails the reservation or order that caused it."""
from collections.abc import Callable

from app.core.database import Database
from app.models import Notification
from app.services import whatsapp

# Each takes the database handle (in the caller's transaction) and the in-app notification just written, and writes
# that channel's outbox row when the recipient opted in.
OUTBOX_CHANNELS: tuple[Callable[[Database, Notification], object], ...] = (whatsapp.enqueue,)


def notify(db: Database, user_id: int, kind: str, title: str, body: str | None = None, link: str | None = None) -> None:
    note = db.notifications.insert(Notification(user_id=user_id, kind=kind, title=title, body=body, link=link))
    for enqueue in OUTBOX_CHANNELS:
        enqueue(db, note)
