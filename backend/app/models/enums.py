"""Enumerations shared by models, schemas and services."""
from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    OWNER = "owner"
    ADMIN = "admin"
    STAFF = "staff"  # counter staff of one shop, invited by its owner (P9.13, app/services/staff.py)


class StaffStatus(StrEnum):
    """A shop_staff row (P9.13). invited -> active on the invitee's first phone sign-in; suspended while the shop's
    plan allows fewer logins (reactivated on renewal); removed by the owner, for good."""

    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class ReservationStatus(StrEnum):
    """Pickup reservation lifecycle.

    REQUESTED -> CONFIRMED -> READY_FOR_PICKUP -> COMPLETED
    Side exits: REJECTED (shop declines), CANCELLED (customer or shop), EXPIRED (timer ran out).
    Stock is held (decremented) when the shop confirms, and released on any side exit.
    """

    REQUESTED = "REQUESTED"
    CONFIRMED = "CONFIRMED"
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class OrderStatus(StrEnum):
    """Shop-managed delivery lifecycle.

    PENDING -> SHOP_CONFIRMED -> PREPARING -> OUT_FOR_DELIVERY -> DELIVERED
    Failure paths: CANCELLED (before dispatch), DELIVERY_FAILED -> RETURNED_TO_SHOP.
    """

    PENDING = "PENDING"
    SHOP_CONFIRMED = "SHOP_CONFIRMED"
    PREPARING = "PREPARING"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    DELIVERY_FAILED = "DELIVERY_FAILED"
    RETURNED_TO_SHOP = "RETURNED_TO_SHOP"


class InventoryReason(StrEnum):
    INITIAL = "initial"
    RESTOCK = "restock"
    ADJUSTMENT = "adjustment"
    RESERVATION_HOLD = "reservation_hold"
    RESERVATION_RELEASE = "reservation_release"
    ORDER_HOLD = "order_hold"
    ORDER_RELEASE = "order_release"
    WHATSAPP_UPDATE = "whatsapp_update"  # the owner set a count by replying on WhatsApp (P4.4)


class AnomalyStatus(StrEnum):
    OPEN = "open"
    REVIEWED = "reviewed"
    DISMISSED = "dismissed"


class SaleSource(StrEnum):
    RESERVATION = "reservation"
    ORDER = "order"
    DEMO = "demo"
    IMPORT = "import"  # a sales report uploaded from the shop's billing app (P7.2; services/importer.py)
