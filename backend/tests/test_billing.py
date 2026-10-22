"""Payments to NearShop's own account, receipts and invoices (P9.5; app/services/billing.py; plan section 8.18).

The tests use their own shops and owners, a test UPI ID and payee name (never real ones), and, where the rules depend
on dates, a controlled clock (the now passed to the service). Everything they write is removed afterwards. Billing
starts on 1 Aug 2027 (NEARSHOP_BILLING_STARTS_ON), so a payment made before it is an advance."""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app.core.config import settings
from app.core.database import Database, utcnow
from app.core.security import hash_password
from app.main import app
from app.models import Role, Shop, User
from app.models.billing import Actor, SubscriptionStatus
from app.models.core import VerificationStatus, months_after
from app.services import billing, plans
from tests.conftest import CENTER, HEADERS, PASSWORD, login

IST = ZoneInfo("Asia/Kolkata")
PW_HASH = hash_password(PASSWORD)
ADMIN = Actor(type="admin", id=0)
REASON = "Matched from the bank statement by hand"
TEST_GSTIN = "29AAAAA0000A1Z5"  # the GSTIN format only; not a real registration
OTHER_STATE_GSTIN = "27AAAAA0000A1Z5"
NOTICE_KINDS = [billing.KIND_PAYMENT_RECEIVED, billing.KIND_REFUNDED, billing.KIND_UNMATCHED,
                plans.KIND_ACTIVATED, plans.KIND_LAPSED]
BILLING_COLLECTIONS = (billing.ACCOUNTS, billing.REQUESTS, billing.CREDITS, billing.PAYMENTS, billing.DOCUMENTS,
                       billing.STATEMENTS)


def ist(*args: int) -> datetime:
    """An IST wall-clock time as the naive UTC time the database stores."""
    return datetime(*args, tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def _clean(db: Database) -> None:
    for name in (*BILLING_COLLECTIONS, plans.SUBSCRIPTIONS, plans.EVENTS):
        db.mongo[name].delete_many({})
    db.mongo["counters"].delete_many({"_id": {"$regex": f"^({billing.SERIES_PREFIX}|{plans.FOUNDING_COUNTER}$)"}})
    db.notifications.delete_many({"kind": {"$in": NOTICE_KINDS}})


@pytest.fixture(autouse=True)
def clean(ids, monkeypatch):
    # A test merchant UPI ID and payee name: payment requests need them (never a real handle in the repo).
    monkeypatch.setattr(settings, "billing_upi_id", "nearshop-test@testbank")
    monkeypatch.setattr(settings, "billing_payee_name", "NearShop Test")
    db = Database()
    _clean(db)
    yield db
    _clean(db)


class World:
    """Shops with their own owners, removed afterwards."""

    def __init__(self, db: Database):
        self.db = db
        self.tag = uuid.uuid4().hex[:8]
        self.users: list[int] = []
        self.shops: list[int] = []

    def shop(self, n: int, city: str = "Ballari") -> Shop:
        owner = self.db.users.insert(User(name=f"Billing Owner {n}", email=f"bill-owner{n}-{self.tag}@example.com",
                                          password_hash=PW_HASH, role=Role.OWNER))
        self.users.append(owner.id)
        now = utcnow()
        shop = self.db.shops.insert(Shop(
            owner_id=owner.id, name=f"Billing Shop {n} {self.tag}", slug=f"billing-shop-{n}-{self.tag}",
            address_line=f"{n} Bill Road", locality="Cowl Bazaar", city=city, pincode="583101",
            lat=CENTER[0] + 0.003, lng=CENTER[1], category_ids=[], verification_status=VerificationStatus.VERIFIED,
            is_verified=True, kyc_type="udyam", kyc_number="UDYAM-KR-23-0000888", phone_verified_at=now,
            submitted_at=now, decided_at=now, verified_until=months_after(now)))
        self.shops.append(shop.id)
        return shop

    def email(self, shop: Shop) -> str:
        return self.db.users.get(shop.owner_id).email

    def cleanup(self) -> None:
        self.db.notifications.delete_many({"user_id": {"$in": self.users}})
        self.db.shops.delete_many({"_id": {"$in": self.shops}})
        self.db.users.delete_many({"_id": {"$in": self.users}})


@pytest.fixture
def world(db):
    w = World(db)
    yield w
    w.cleanup()


def request_for(db: Database, shop: Shop, **kw) -> billing.PaymentRequest:
    return billing.create_request(db, shop, plan_code=kw.pop("plan_code", "growth"), actor=ADMIN, **kw)


def pay_by_hand(db: Database, req: billing.PaymentRequest, ref: str, on: date = date(2027, 6, 2),
                now: datetime | None = None, amount: int | None = None) -> billing.Payment:
    return billing.match_by_hand(db, upi_reference=ref, request_number=req.number, reason=REASON, actor=ADMIN,
                                 amount_paise=amount or req.total_paise, credited_on=on, now=now)


def statement(rows: list[tuple[str, str, str, str, str]]) -> bytes:
    """A statement export in the documented layout: preamble lines, a header row, then date, narration, reference,
    debit and credit."""
    lines = ["NearShop Technologies Pvt Ltd - Current Account", "Statement from 01-06-2027 to 30-06-2027", "",
             "Txn Date,Value Date,Description,Ref No./Cheque No.,Debit,Credit,Balance"]
    lines += [f'{d},{d},"{narr}",{ref},{debit},"{credit}",1000.00' for d, narr, ref, debit, credit in rows]
    lines += ["", "Closing balance,,,,,,1000.00"]
    return "\n".join(lines).encode()


def subscription_of(db: Database, shop: Shop) -> plans.Subscription | None:
    return plans.subscriptions(db).find_one({"shop_ids": shop.id})


def doc_of(db: Database, pay: billing.Payment) -> billing.BillingDocument:
    return billing.documents(db).get(billing.payments(db).get(pay.id).document_id)


# ---------------------------------------------------------------- payment requests


def test_the_owner_sees_nearshops_upi_id_payee_and_the_request_number_as_the_note(db, world):
    shop = world.shop(1)
    with TestClient(app) as c:
        owner = login(c, world.email(shop))
        assert owner.get("/api/owner/billing", headers=HEADERS).json()["requests"] == []
        res = owner.post("/api/owner/billing/requests", json={"plan_code": "growth"}, headers=HEADERS)
        assert res.status_code == 200, res.text
        req = res.json()
        # Founding price for a Ballari shop, before billing starts: an advance, no GST while unregistered.
        assert (req["kind"], req["founding"], req["amount_paise"], req["gst_paise"], req["total_paise"]) == \
            ("advance", True, 99_900, 0, 99_900)
        assert req["pay_to"] == {"upi_id": "nearshop-test@testbank", "payee_name": "NearShop Test", "amount": "999.00",
                                 "note": req["number"]}
        assert req["number"].startswith("NSP") and len(req["number"]) == 9
        again = owner.post("/api/owner/billing/requests", json={"plan_code": "growth"}, headers=HEADERS).json()
        assert again["number"] != req["number"]  # a new request cancels the open one
        assert billing.requests(db).get(req["id"]).status == billing.RequestStatus.CANCELLED
        assert [r["number"] for r in owner.get("/api/owner/billing", headers=HEADERS).json()["requests"]] == \
            [again["number"]]
        assert owner.post("/api/owner/billing/requests", json={"plan_code": "growth", "period": "month"},
                          headers=HEADERS).status_code == 422  # monthly plans are UPI Autopay (P9.6)
        account = owner.put("/api/owner/billing/account", headers=HEADERS, json={
            "legal_name": "Sri Billing Traders", "address": "1 Bill Road, Ballari", "state_code": "29",
            "gstin": TEST_GSTIN})
        assert account.status_code == 200 and account.json()["gstin"] == TEST_GSTIN
        assert owner.put("/api/owner/billing/account", headers=HEADERS, json={
            "legal_name": "Sri Billing Traders", "address": "1 Bill Road, Ballari", "state_code": "29",
            "gstin": OTHER_STATE_GSTIN}).status_code == 422  # a GSTIN starts with its state code


def test_payment_requests_are_refused_until_the_upi_id_is_set(db, world, monkeypatch):
    monkeypatch.setattr(settings, "billing_upi_id", "")
    with pytest.raises(billing.BillingNotConfigured):
        request_for(db, world.shop(1))


# ---------------------------------------------------------------- documents


def test_an_unregistered_payment_gets_a_receipt_that_charges_no_gst(db, world, admin):
    shop = world.shop(1, city="Mysuru")
    req = request_for(db, shop)
    assert (req.amount_paise, req.total_paise, req.gst_mode) == (149_900, 149_900, billing.GstMode.UNREGISTERED)
    pay = pay_by_hand(db, req, "612345678901")
    doc = doc_of(db, pay)
    assert doc.type == billing.DocType.RECEIPT
    assert (doc.taxable_paise, doc.cgst_paise, doc.sgst_paise, doc.igst_paise, doc.total_paise) == \
        (149_900, 0, 0, 0, 149_900)
    assert doc.sac is None and doc.supplier.gstin is None
    assert doc.recipient.legal_name == shop.name and doc.recipient.state_code == "29"
    assert doc.payment_date == ist(2027, 6, 2) and doc.request_number == req.number
    # Draft format until the CA approves one (B22): a visible draft line, never wording the team wrote.
    assert doc.draft_line == "Draft format, not for use" and doc.wording == billing.WORDING_PENDING
    assert len(doc.number) <= 16
    shown = admin.get(f"/api/admin/billing/documents/{doc.id}", headers=HEADERS).json()
    assert shown["number"] == doc.number and shown["intact"] is True
    sub = subscription_of(db, shop)
    assert (sub.status, sub.source.value, sub.price_paise) == (SubscriptionStatus.ADVANCE, "advance", 149_900)
    assert billing.credits(db).find_one({"upi_reference": "612345678901"}).status == billing.CreditStatus.MATCHED
    assert billing.requests(db).get(req.id).status == billing.RequestStatus.PAID


@pytest.mark.parametrize("gstin, cgst, sgst, igst", [
    (TEST_GSTIN, 13_491, 13_491, 0),  # a Karnataka payer: CGST and SGST
    (OTHER_STATE_GSTIN, 0, 0, 26_982),  # another state: IGST
])
def test_a_registered_payment_gets_a_tax_invoice_split_by_the_payers_state(db, world, monkeypatch, gstin, cgst,
                                                                             sgst, igst):
    monkeypatch.setattr(settings, "billing_gst_mode", "registered")
    monkeypatch.setattr(settings, "billing_gst_registered_from", date(2026, 1, 1))
    monkeypatch.setattr(settings, "billing_gstin", TEST_GSTIN)
    monkeypatch.setattr(settings, "billing_sac", "998599")
    shop = world.shop(1, city="Mysuru")
    billing.update_account(db, shop.owner_id, legal_name="Payer Traders", address="2 Bill Road",
                           state_code=gstin[:2], gstin=gstin, shop=shop)
    req = request_for(db, shop)
    assert (req.amount_paise, req.gst_paise, req.total_paise) == (149_900, 26_982, 176_882)
    doc = doc_of(db, pay_by_hand(db, req, "612345678902"))
    assert doc.type == billing.DocType.TAX_INVOICE
    assert (doc.taxable_paise, doc.gst_rate_pct, doc.cgst_paise, doc.sgst_paise, doc.igst_paise, doc.total_paise) == \
        (149_900, 18, cgst, sgst, igst, 176_882)
    assert doc.supplier.gstin == TEST_GSTIN and doc.recipient.gstin == gstin and doc.sac == "998599"
    assert doc.place_of_supply.startswith(gstin[:2])
    # The subscription is priced before GST.
    assert subscription_of(db, shop).price_paise == 149_900


def test_a_document_cannot_be_changed_after_it_is_issued(db, world, admin):
    doc = doc_of(db, pay_by_hand(db, request_for(db, world.shop(1)), "612345678903"))
    with pytest.raises(billing.DocumentImmutable):
        billing.documents(db).set(doc, total_paise=1)
    with pytest.raises(billing.DocumentImmutable):
        billing.documents(db).update_one({"_id": doc.id}, {"$set": {"total_paise": 1}})
    with pytest.raises(billing.DocumentImmutable):
        billing.documents(db).delete_many({"_id": doc.id})
    for method in ("put", "patch", "delete"):  # no route changes a document
        assert getattr(admin, method)(f"/api/admin/billing/documents/{doc.id}", headers=HEADERS).status_code == 405
    assert billing.verify_document(billing.documents(db).get(doc.id))
    # A change made behind the app's back is detected by the content hash.
    db.mongo[billing.DOCUMENTS].update_one({"_id": doc.id}, {"$set": {"total_paise": 1}})
    assert not billing.verify_document(billing.documents(db).get(doc.id))
    assert admin.get(f"/api/admin/billing/documents/{doc.id}", headers=HEADERS).json()["intact"] is False


# ---------------------------------------------------------------- numbering


def test_numbers_have_no_gaps_or_duplicates_under_concurrent_payments(db, world):
    shops = [world.shop(n, city="Mysuru") for n in range(8)]
    reqs = [request_for(db, s) for s in shops]

    def pay(i: int) -> int:
        return pay_by_hand(Database(), reqs[i], f"61234567{i:04d}").id

    with ThreadPoolExecutor(max_workers=8) as pool:
        paid = list(pool.map(pay, range(8)))
    docs = billing.documents(db).find({"payment_id": {"$in": paid}}, sort=[("seq", 1)])
    fy = billing.financial_year(utcnow())
    assert [d.seq for d in docs] == list(range(1, 9))
    assert [d.number for d in docs] == [f"NS/{fy[2:4]}-{fy[5:7]}/{n:06d}" for n in range(1, 9)]
    assert all(len(d.number) <= 16 for d in docs)


def test_a_new_series_starts_on_1_april(db, world):
    shops = [world.shop(n, city="Mysuru") for n in range(3)]
    reqs = [request_for(db, s) for s in shops]
    march = pay_by_hand(db, reqs[0], "612345670001", on=date(2027, 3, 31), now=ist(2027, 3, 31, 23, 59))
    april = pay_by_hand(db, reqs[1], "612345670002", on=date(2027, 4, 1), now=ist(2027, 4, 1, 0, 1))
    later = pay_by_hand(db, reqs[2], "612345670003", on=date(2027, 4, 2), now=ist(2027, 4, 2, 10, 0))
    assert [doc_of(db, p).number for p in (march, april, later)] == \
        ["NS/26-27/000001", "NS/27-28/000001", "NS/27-28/000002"]
    assert [doc_of(db, p).financial_year for p in (march, april, later)] == ["2026-27", "2027-28", "2027-28"]


# ---------------------------------------------------------------- rollback and the draft format


def test_a_failure_while_writing_the_document_rolls_back_the_payment_and_the_subscription(db, world, monkeypatch):
    shop = world.shop(1)
    req = request_for(db, shop)
    credit = billing.credits(db).insert(billing.BankCredit(
        upi_reference="612345679999", credited_at=ist(2027, 6, 2), amount_paise=req.total_paise,
        source=billing.CreditSource.STATEMENT, request_number=req.number))

    def broken(self, doc):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr(billing._Documents, "insert", broken)
    with pytest.raises(RuntimeError):
        billing.match_by_hand(db, upi_reference=credit.upi_reference, request_number=req.number, reason=REASON,
                              actor=ADMIN)
    assert subscription_of(db, shop) is None
    assert db.mongo[plans.EVENTS].count_documents({"shop_ids": shop.id}) == 0
    assert billing.payments(db).count({}) == 0 and billing.documents(db).count({}) == 0
    assert billing.credits(db).get(credit.id).status == billing.CreditStatus.UNMATCHED
    assert billing.requests(db).get(req.id).status == billing.RequestStatus.OPEN
    assert plans.founding_taken(db) == 0  # the founding slot was given back
    series = db.mongo["counters"].find_one({"_id": billing.SERIES_PREFIX + billing.financial_year(utcnow())})
    assert series["seq"] == 0  # no number used
    monkeypatch.undo()
    monkeypatch.setattr(settings, "billing_upi_id", "nearshop-test@testbank")
    monkeypatch.setattr(settings, "billing_payee_name", "NearShop Test")
    pay = billing.match_by_hand(db, upi_reference=credit.upi_reference, request_number=req.number, reason=REASON,
                                actor=ADMIN)
    assert doc_of(db, pay).seq == 1 and subscription_of(db, shop).founding_slot == 1


def test_production_refuses_to_issue_a_document_in_the_draft_format(db, world, monkeypatch):
    shop = world.shop(1)
    req = request_for(db, shop)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(billing.BillingNotConfigured) as refused:
        pay_by_hand(db, req, "612345670010")
    assert refused.value.code == "document_format_not_approved"
    assert subscription_of(db, shop) is None and billing.credits(db).count({}) == 0
    # A statement credit stays in the unmatched queue with the reason.
    result = billing.import_statement(db, statement([("02-06-2027", f"UPI/612345670011/{req.number}", "612345670011",
                                                      "", "999.00")]), "june.csv", actor=ADMIN)
    assert (result["matched"], result["unmatched"]) == (0, 1)
    assert "draft" in result["rows"][0]["reason"]
    # The CA's approved format and wording (set from B22) are issued, without the draft line.
    monkeypatch.setattr(settings, "billing_document_format", "CA-approved v1")
    monkeypatch.setattr(settings, "billing_wording_receipt", "Approved receipt wording")
    monkeypatch.setattr(settings, "billing_legal_name", "NearShop Test Company")
    monkeypatch.setattr(settings, "billing_address", "Test address, Ballari")
    credit = billing.credits(db).find_one({"upi_reference": "612345670011"})
    pay = billing.match_by_hand(db, upi_reference=credit.upi_reference, request_number=req.number, reason=REASON,
                                actor=ADMIN)
    doc = doc_of(db, pay)
    assert (doc.format, doc.draft_line, doc.wording) == ("CA-approved v1", None, "Approved receipt wording")
    assert doc.supplier.legal_name == "NearShop Test Company"


# ---------------------------------------------------------------- refunds


def test_a_refund_references_its_original_and_refunds_the_subscription(db, world, admin):
    shop = world.shop(1)
    pay = pay_by_hand(db, request_for(db, shop), "612345670020")
    original = doc_of(db, pay)
    res = admin.post(f"/api/admin/billing/payments/{pay.id}/refund", headers=HEADERS,
                     json={"bank_reference": "NEFT-TEST-0001", "reason": "The shop asked before billing started"})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["status"] == "refunded" and out["refund"]["bank_reference"] == "NEFT-TEST-0001"
    refund_doc = billing.documents(db).get(out["refund"]["document_id"])
    assert refund_doc.type == billing.DocType.REFUND_RECEIPT
    assert (refund_doc.original_document_id, refund_doc.original_number) == (original.id, original.number)
    assert refund_doc.total_paise == original.total_paise and refund_doc.seq == original.seq + 1
    sub = subscription_of(db, shop)
    assert sub.status == SubscriptionStatus.REFUNDED
    kinds = [e["kind"] for e in db.mongo[plans.EVENTS].find({"subscription_id": sub.id}).sort("_id", 1)]
    assert kinds == ["created", "refunded"]
    assert plans.effective_plan(db, shop.id)["code"] == "free"
    assert billing.requests(db).get(pay.request_id).status == billing.RequestStatus.REFUNDED
    again = admin.post(f"/api/admin/billing/payments/{pay.id}/refund", headers=HEADERS,
                       json={"bank_reference": "NEFT-TEST-0002", "reason": "A second refund by mistake"})
    assert again.status_code == 409


def test_refunding_a_renewal_takes_off_only_its_term_and_payments_are_refunded_latest_first(db, world, monkeypatch):
    monkeypatch.setattr(settings, "billing_starts_on", date(2020, 1, 1))
    shop = world.shop(1, city="Mysuru")
    first = pay_by_hand(db, request_for(db, shop), "612345670030", on=date(2026, 10, 1))
    one_year = plans.subscriptions(db).get(first.subscription_id)
    second = pay_by_hand(db, request_for(db, shop), "612345670031", on=date(2026, 10, 2))
    assert plans.subscriptions(db).get(one_year.id).ends_at == months_after(one_year.ends_at, 12)

    with pytest.raises(billing.BillingRefused) as refused:  # the first year is refunded after the renewal, not before
        billing.refund_payment(db, first.id, reason="Refund asked for", bank_reference="NEFT-TEST-0010", actor=ADMIN)
    assert refused.value.code == "refund_later_first"
    assert billing.payments(db).get(first.id).status == billing.PaymentStatus.MATCHED

    billing.refund_payment(db, second.id, reason="Renewed by mistake", bank_reference="NEFT-TEST-0011", actor=ADMIN)
    sub = plans.subscriptions(db).get(one_year.id)
    # The shop keeps the first year it paid for: only the renewal's year is taken off.
    assert (sub.status, sub.ends_at, sub.paid_until) == (SubscriptionStatus.ACTIVE, one_year.ends_at,
                                                         one_year.paid_until)
    assert sub.paid_terms[-1].end == one_year.ends_at
    assert plans.effective_plan(db, shop.id)["code"] == "growth"
    assert billing.requests(db).get(first.request_id).status == billing.RequestStatus.PAID

    refunded = billing.refund_payment(db, first.id, reason="Shop closed for good", bank_reference="NEFT-TEST-0012",
                                      actor=ADMIN)
    assert refunded.status == billing.PaymentStatus.REFUNDED
    assert plans.subscriptions(db).get(one_year.id).status == SubscriptionStatus.REFUNDED
    assert plans.effective_plan(db, shop.id)["code"] == "free"
    kinds = [e["kind"] for e in db.mongo[plans.EVENTS].find({"subscription_id": one_year.id}).sort("_id", 1)]
    assert kinds == ["created", "renewed", "renewal_refunded", "refunded"]
    notes = [billing.documents(db).get(billing.payments(db).get(p.id).refund.document_id) for p in (second, first)]
    assert [n.original_number for n in notes] == [doc_of(db, second).number, doc_of(db, first).number]


def test_a_tax_invoice_is_refunded_with_a_credit_note(db, world, monkeypatch):
    monkeypatch.setattr(settings, "billing_gst_mode", "registered")
    monkeypatch.setattr(settings, "billing_gst_registered_from", date(2026, 1, 1))
    monkeypatch.setattr(settings, "billing_gstin", TEST_GSTIN)
    pay = pay_by_hand(db, request_for(db, world.shop(1, city="Mysuru")), "612345670021")
    refunded = billing.refund_payment(db, pay.id, reason="Shop closed for good", bank_reference="NEFT-TEST-0003",
                                      actor=ADMIN)
    note = billing.documents(db).get(refunded.refund.document_id)
    original = doc_of(db, pay)
    assert note.type == billing.DocType.CREDIT_NOTE and note.original_number == original.number
    assert (note.cgst_paise, note.sgst_paise, note.total_paise) == (original.cgst_paise, original.sgst_paise,
                                                                    original.total_paise)


# ---------------------------------------------------------------- statement matching


def test_statement_matching_exact_unmatched_and_duplicate(db, world, admin):
    shops = [world.shop(n, city="Mysuru") for n in range(2)]
    a, b = (request_for(db, s) for s in shops)
    rows = [
        ("02-06-2027", f"UPI/612300000001/NearShop/{a.number}", "612300000001", "", "1,499.00"),  # exact
        ("03-06-2027", f"UPI/612300000002/{b.number}", "612300000002", "", "1,000.00"),  # wrong amount
        ("03-06-2027", "UPI/612300000003/payment", "612300000003", "", "1,499.00"),  # no request number
        ("04-06-2027", "NEFT charges", "CHG0001", "50.00", ""),  # a debit
    ]
    res = admin.post("/api/admin/billing/statements", headers=HEADERS,
                     files={"file": ("june.csv", statement(rows), "text/csv")})
    assert res.status_code == 200, res.text
    out = res.json()
    assert (out["credits"], out["matched"], out["unmatched"], out["duplicates"], out["debits_skipped"]) == \
        (3, 1, 2, 0, 1)
    assert [r["result"] for r in out["rows"]] == ["matched", "unmatched", "unmatched"]
    assert "not the request's" in out["rows"][1]["reason"]
    assert subscription_of(db, shops[0]).status == SubscriptionStatus.ADVANCE
    assert subscription_of(db, shops[1]) is None
    queue = admin.get("/api/admin/billing/credits", headers=HEADERS).json()
    assert [c["upi_reference"] for c in queue] == ["612300000002", "612300000003"]

    again = admin.post("/api/admin/billing/statements", headers=HEADERS,
                       files={"file": ("june-again.csv", statement(rows[:1]), "text/csv")}).json()
    assert (again["matched"], again["duplicates"]) == (0, 1)
    assert again["rows"][0]["status"] == "matched"  # a credit already matched is reported as a duplicate
    assert billing.payments(db).count({}) == 1

    # The unmatched credit with no number is matched by hand, by its UPI reference, with a reason.
    res = admin.post("/api/admin/billing/credits/match", headers=HEADERS, json={
        "upi_reference": "612300000003", "request_number": b.number, "reason": "The owner forgot the payment note"})
    assert res.status_code == 200, res.text
    assert res.json()["match"] == "manual" and subscription_of(db, shops[1]) is not None
    # The short payment is returned to the payer by bank transfer.
    short = billing.credits(db).find_one({"upi_reference": "612300000002"})
    res = admin.post(f"/api/admin/billing/credits/{short.id}/return", headers=HEADERS,
                     json={"bank_reference": "NEFT-TEST-0100", "reason": "Paid the wrong amount; sent back"})
    assert res.status_code == 200 and res.json()["status"] == "returned"
    assert admin.get("/api/admin/billing/credits", headers=HEADERS).json() == []


def test_a_planted_statement_of_20_credits_gives_18_matches_1_unmatched_and_1_duplicate(db, world):
    shops = [world.shop(n) for n in range(18)]  # Ballari: each takes a founding slot, in order of payment
    reqs = [request_for(db, s) for s in shops]
    rows = [(f"{n + 1:02d}-06-2027", f"UPI/6123{n:08d}/{r.number}/NearShop", f"6123{n:08d}", "", "999.00")
            for n, r in enumerate(reqs)]
    rows.insert(9, ("10-06-2027", "UPI/612399999999/for shop plan", "612399999999", "", "999.00"))  # unmatched
    rows.append(("20-06-2027", rows[0][1], rows[0][2], "", "999.00"))  # the first credit again: a duplicate
    rows.insert(5, ("05-06-2027", "ATM withdrawal", "ATM0001", "2,000.00", ""))  # a debit, not a credit
    result = billing.import_statement(db, statement(rows), "planted.csv", actor=ADMIN)
    assert (result["credits"], result["matched"], result["unmatched"], result["duplicates"]) == (20, 18, 1, 1)
    assert result["debits_skipped"] == 1
    assert plans.subscriptions(db).count({"status": "advance"}) == 18 and plans.founding_taken(db) == 18
    assert sorted(d.seq for d in billing.documents(db).find({})) == list(range(1, 19))
    stored = billing.statements(db).get(result["statement_id"])
    assert (stored.matched, stored.unmatched, stored.duplicates) == (18, 1, 1)


# ---------------------------------------------------------------- the pilot ledger


def test_the_pilot_ledger_import_and_its_refusal_of_a_repeated_upi_reference(db, world, admin):
    first, second = world.shop(1), world.shop(2)
    body = {"reason": "R1's hand-kept ledger of pilot advances", "rows": [
        {"paid_on": "2026-06-05", "shop_id": second.id, "amount_paise": 99_900, "upi_reference": "612355550002"},
        {"paid_on": "2026-06-01", "shop_id": first.id, "amount_paise": 99_900, "upi_reference": "612355550001"},
    ]}
    res = admin.post("/api/admin/billing/pilot-ledger", json=body, headers=HEADERS)
    assert res.status_code == 200, res.text
    assert res.json()["imported"] == 2
    for shop, paid_on, slot in ((first, date(2026, 6, 1), 1), (second, date(2026, 6, 5), 2)):
        sub = subscription_of(db, shop)
        # In order of payment: the shop that paid first has founding slot 1.
        assert (sub.status, sub.founding_slot, sub.paid_at) == (SubscriptionStatus.ADVANCE, slot,
                                                                billing.leads.ist_start(paid_on))
        pay = billing.payments(db).find_one({"shop_id": shop.id})
        assert pay.match == billing.CreditSource.PILOT_LEDGER
        doc = billing.documents(db).get(pay.document_id)
        assert doc.type == billing.DocType.RECEIPT and doc.payment_date == billing.leads.ist_start(paid_on)

    third = world.shop(3)
    repeated = admin.post("/api/admin/billing/pilot-ledger", headers=HEADERS, json={
        "reason": "R1's ledger, second page", "rows": [
            {"paid_on": "2026-06-07", "shop_id": third.id, "amount_paise": 99_900, "upi_reference": "612355550003"},
            {"paid_on": "2026-06-08", "shop_id": third.id, "amount_paise": 99_900, "upi_reference": "612355550001"}]})
    assert repeated.status_code == 409 and repeated.json()["error"]["code"] == "repeated_reference"
    in_file = admin.post("/api/admin/billing/pilot-ledger", headers=HEADERS, json={
        "reason": "R1's ledger, second page", "rows": [
            {"paid_on": "2026-06-07", "shop_id": third.id, "amount_paise": 99_900, "upi_reference": "612355550004"},
            {"paid_on": "2026-06-08", "shop_id": third.id, "amount_paise": 99_900, "upi_reference": "612355550004"}]})
    assert in_file.status_code == 409
    assert subscription_of(db, third) is None and billing.payments(db).count({}) == 2
    wrong = admin.post("/api/admin/billing/pilot-ledger", headers=HEADERS, json={
        "reason": "R1's ledger, second page", "rows": [
            {"paid_on": "2026-06-07", "shop_id": third.id, "amount_paise": 50_000, "upi_reference": "612355550005"}]})
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "ledger_amount"
    assert billing.credits(db).find_one({"upi_reference": "612355550005"}) is None


def test_the_pilot_ledger_refuses_a_payment_date_later_than_today(db, world, admin):
    shop = world.shop(1)
    tomorrow = (datetime.now(IST).date() + timedelta(days=1)).isoformat()
    res = admin.post("/api/admin/billing/pilot-ledger", headers=HEADERS, json={
        "reason": "R1's hand-kept ledger of pilot advances", "rows": [
            {"paid_on": tomorrow, "shop_id": shop.id, "amount_paise": 99_900, "upi_reference": "612355550006"}]})
    assert res.status_code == 422
    with pytest.raises(billing.BillingRefused) as refused:
        billing.import_pilot_ledger(db, [{"paid_on": date(2026, 6, 10), "shop_id": shop.id, "amount_paise": 99_900,
                                          "upi_reference": "612355550007"}], reason="R1's ledger", actor=ADMIN,
                                    now=ist(2026, 6, 9, 12))
    assert refused.value.code == "ledger_future_date"
    assert subscription_of(db, shop) is None and billing.credits(db).count({}) == 0


# ---------------------------------------------------------------- renewals


def test_a_renewal_payment_extends_the_active_subscription(db, world, monkeypatch):
    monkeypatch.setattr(settings, "billing_starts_on", date(2020, 1, 1))
    shop = world.shop(1, city="Mysuru")
    first = pay_by_hand(db, request_for(db, shop), "612366660001", on=date(2026, 10, 1))
    sub = plans.subscriptions(db).get(first.subscription_id)
    assert (sub.status, sub.source.value) == (SubscriptionStatus.ACTIVE, "shop_paid")
    renewal = request_for(db, shop)
    assert (renewal.kind, renewal.subscription_id) == (billing.RequestKind.RENEWAL, sub.id)
    pay_by_hand(db, renewal, "612366660002", on=date(2026, 10, 2))
    renewed = plans.subscriptions(db).get(sub.id)
    assert renewed.ends_at == months_after(sub.ends_at, 12)
    with pytest.raises(billing.BillingRefused):
        request_for(db, shop, plan_code="pro")  # Pro is not on sale


def test_an_advance_paid_before_billing_starts_stays_an_advance_when_matched_after_it(db, world):
    early, late = world.shop(1), world.shop(2)
    on_time = request_for(db, early, now=ist(2027, 7, 30, 12))
    too_late = request_for(db, late, now=ist(2027, 7, 30, 12))
    assert on_time.kind == too_late.kind == billing.RequestKind.ADVANCE
    # Paid on 31 Jul, matched from the statement on 2 Aug: still the advance it was paid as.
    pay = pay_by_hand(db, on_time, "612388880001", on=date(2027, 7, 31), now=ist(2027, 8, 2, 10))
    sub = plans.subscriptions(db).get(pay.subscription_id)
    assert (sub.source.value, sub.status, sub.paid_at) == ("advance", SubscriptionStatus.ADVANCE,
                                                           billing.leads.ist_start(date(2027, 7, 31)))
    assert "advance payment for" in doc_of(db, pay).description
    plans.activate_advances(db, ist(2027, 8, 2, 10, 5))
    sub = plans.subscriptions(db).get(pay.subscription_id)
    assert (sub.status, sub.starts_at) == (SubscriptionStatus.ACTIVE, plans.billing_start())
    # Paid on 2 Aug, after billing started: a shop-paid plan from then, and its receipt does not call it an advance.
    paid_late = pay_by_hand(db, too_late, "612388880002", on=date(2027, 8, 2), now=ist(2027, 8, 2, 10))
    sub = plans.subscriptions(db).get(paid_late.subscription_id)
    assert (sub.source.value, sub.status) == ("shop_paid", SubscriptionStatus.ACTIVE)
    assert "subscription to" in doc_of(db, paid_late).description


def test_an_admins_reason_for_a_request_made_for_the_owner_is_kept(db, world, admin):
    shop = world.shop(1)
    res = admin.post("/api/admin/billing/requests", headers=HEADERS,
                     json={"shop_id": shop.id, "reason": "Owner asked on the phone to pay for Growth"})
    assert res.status_code == 200, res.text
    assert res.json()["reason"] == "Owner asked on the phone to pay for Growth"
    assert billing.requests(db).get(res.json()["id"]).reason == "Owner asked on the phone to pay for Growth"


# ---------------------------------------------------------------- the daily reconciliation alert


def test_admins_are_alerted_about_a_credit_unmatched_for_3_working_days(db, world, monkeypatch):
    billing.import_statement(db, statement([("07-06-2027", "UPI/612377770001/no note", "612377770001", "",
                                             "999.00")]), "june.csv", actor=ADMIN)  # Monday 7 June 2027
    admin_user = db.users.find_one({"email": "admin@example.com"})

    def alerts() -> int:
        return db.notifications.count({"user_id": admin_user.id, "kind": billing.KIND_UNMATCHED})

    assert billing.alert_unmatched(db, ist(2027, 6, 9, 9))["alerted"] == 0  # Wednesday: 2 working days
    assert billing.alert_unmatched(db, ist(2027, 6, 10, 9))["alerted"] == 1  # Thursday: 3
    assert alerts() == 1
    assert billing.alert_unmatched(db, ist(2027, 6, 11, 9))["alerted"] == 0  # once per credit
    assert alerts() == 1

    # Weekends and the bank holidays listed are not working days.
    monkeypatch.setattr(settings, "billing_bank_holidays", "2027-06-15")
    billing.import_statement(db, statement([("11-06-2027", "UPI/612377770002/no note", "612377770002", "",
                                             "999.00")]), "june2.csv", actor=ADMIN)  # Friday 11 June
    assert billing.alert_unmatched(db, ist(2027, 6, 16, 9))["alerted"] == 0  # Mon 14, Wed 16: 2
    assert billing.alert_unmatched(db, ist(2027, 6, 17, 9))["alerted"] == 1


def test_the_reconciliation_job_runs_once_a_day(db, monkeypatch):
    assert jobs.JOBS[jobs.BILLING_JOB] is jobs.billing_reconciliation
    seen: list[datetime] = []
    monkeypatch.setattr(billing, "alert_unmatched", lambda _db, now: seen.append(now) or {"alerted": 0})
    result = jobs.billing_reconciliation(lambda: ist(2027, 6, 10, 9))
    assert result["ran_for"] == "2027-06-10" and len(seen) == 1
