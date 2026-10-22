"""Sales-report import from billing apps (P7.2): column detection on three generated layouts, preview and commit,
re-upload replacing rather than duplicating, the 90-day limit, rows never marked demo, and the migration."""
import io
import os
import uuid
from datetime import date, datetime, timedelta

import pytest
from pymongo.errors import DuplicateKeyError

from app.core.database import client as mongo_client
from app.models import SaleSource
from app.services import importer
from database import migrate
from database.migrations import m0010_sales_import
from tests.conftest import HEADERS

CHARGER = "Samsung 25W USB-C Fast Travel Charger"
TAPE = "Astral PTFE Thread Seal Tape"


def day(n: int) -> date:
    """n days before today (India time, as the importer's 'today')."""
    return importer._today() - timedelta(days=n)


@pytest.fixture(autouse=True)
def clean(ids, db):
    yield
    db.sales_history.delete_many({"source": SaleSource.IMPORT.value})


@pytest.fixture
def scratch():
    name = f"{os.environ['NEARSHOP_MONGODB_DB']}_si{uuid.uuid4().hex[:8]}"
    yield mongo_client[name]
    mongo_client.drop_database(name)


def preview(client, name: str, data: bytes | str):
    return client.post("/api/owner/import/preview", params={"kind": "sales"}, headers=HEADERS,
                       files={"file": (name, data, "application/octet-stream")})


def commit(client, body: dict):
    return client.post("/api/owner/import/commit", json={"kind": "sales", **body}, headers=HEADERS)


def kept(p: dict) -> dict:
    """The commit body the page sends for a preview: its dates and the included rows."""
    return {"start": p["start"], "end": p["end"], "rows": [r for r in p["rows"] if r["include"]]}


def plain_csv(*lines: tuple[date, str, int, str, float]) -> str:
    out = "Bill Date,Item,Qty,Bill No,Amount\n"
    return out + "".join(f"{d.isoformat()},{item},{qty},{bill},{amount}\n" for d, item, qty, bill, amount in lines)


def tally_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    start, end = day(6), day(2)
    ws.append(["Shop A Hardware"])  # Tally puts the company, report name and period above the header
    ws.append(["Sales Register"])
    ws.append([f"{start:%d-%b-%Y} to {end:%d-%b-%Y}"])
    ws.append([])
    ws.append(["Date", "Particulars", "Voucher Type", "Voucher No.", "Quantity", "Rate", "Value"])
    ws.append([datetime.combine(start, datetime.min.time()), CHARGER, "Sales", 101, "1 nos", 1249.0, 1249.0])
    ws.append([datetime.combine(start, datetime.min.time()), TAPE, "Sales", 101, "3 nos", 40.0, 120.0])
    ws.append([datetime.combine(end, datetime.min.time()), TAPE, "Sales", 102, "2 nos", 40.0, 80.0])
    ws.append([None, "Grand Total", None, None, None, None, 1449.0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def vyapar_csv() -> str:
    d1, d2 = day(5), day(3)
    return ("Date,Invoice No.,Party Name,Item Name,Quantity,Price/Unit,Amount\n"
            f"{d1:%d/%m/%Y},INV-7,Ravi Kumar,{CHARGER},1,1199,1199\n"
            f"{d1:%d/%m/%Y},INV-7,Ravi Kumar,{TAPE},2,39,78\n"
            f"{d2:%d/%m/%Y},INV-8,Cash,Zorbik Flux Widget,1,10,10\n")


# ---------------------------------------------------------------- column detection on three layouts


def test_tally_style_export_is_detected_below_its_title_lines(owner_a, ids):
    res = preview(owner_a, "sales-register.xlsx", tally_xlsx())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kind"] == "sales"
    assert body["columns"] == {"date": "Date", "item": "Particulars", "quantity": "Quantity", "bill": "Voucher No.",
                               "rate": "Rate", "amount": "Value"}
    assert [(r["name"], r["date"], r["quantity"], r["bill"], r["unit_price"]) for r in body["rows"]] == [
        (CHARGER, day(6).isoformat(), 1, "101", 1249.0), (TAPE, day(6).isoformat(), 3, "101", 40.0),
        (TAPE, day(2).isoformat(), 2, "102", 40.0)]  # the Grand Total line is not a sale
    assert [r["row"] for r in body["rows"]] == [6, 7, 8]  # line numbers in the file, below the title lines
    assert {r["product_id"] for r in body["rows"]} == {ids["charger"], ids["tape"]}
    assert body["start"] == day(6).isoformat() and body["end"] == day(2).isoformat()


def test_tally_item_wise_lines_take_the_date_and_voucher_of_the_bill_above(owner_a, ids):
    # Tally's item-wise register writes the date and voucher number on a bill's first line only, and ends with a
    # Grand Total line that carries the quantity total.
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    first, second = (datetime.combine(day(n), datetime.min.time()) for n in (4, 3))
    ws.append(["Shop A Hardware"])
    ws.append(["Date", "Particulars", "Voucher No.", "Billed Qty", "GST Rate", "Rate", "Taxable Value", "Amount"])
    ws.append([first, CHARGER, "S-1", 1, 18, 1249, 1058.47, 1249])
    ws.append([None, TAPE, None, 2, 18, 40, 67.8, 80])
    ws.append([second, TAPE, "S-2", 1, 18, 40, 33.9, 40])
    ws.append([None, CHARGER, None, 1, 18, 1249, 1058.47, 1249])
    ws.append([None, "Grand Total", None, 5, None, None, 2218.64, 2618])
    buf = io.BytesIO()
    wb.save(buf)
    body = preview(owner_a, "item-register.xlsx", buf.getvalue()).json()
    assert body["columns"]["rate"] == "Rate" and body["columns"]["amount"] == "Amount"  # never the GST columns
    assert [(r["row"], r["date"], r["bill"], r["quantity"], r["unit_price"], r["status"]) for r in body["rows"]] == [
        (3, day(4).isoformat(), "S-1", 1, 1249, "matched"), (4, day(4).isoformat(), "S-1", 2, 40, "matched"),
        (5, day(3).isoformat(), "S-2", 1, 40, "matched"), (6, day(3).isoformat(), "S-2", 1, 1249, "matched")]


def test_vyapar_style_export_is_detected(owner_a, ids):
    res = preview(owner_a, "sale-report.csv", vyapar_csv())
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["columns"] == {"date": "Date", "item": "Item Name", "quantity": "Quantity", "bill": "Invoice No.",
                               "rate": "Price/Unit", "amount": "Amount"}  # Party Name is never the item
    rows = {r["name"]: r for r in body["rows"]}
    assert rows[CHARGER]["date"] == day(5).isoformat() and rows[CHARGER]["unit_price"] == 1199
    assert rows[CHARGER]["status"] == "matched" and rows[CHARGER]["product_id"] == ids["charger"]
    unknown = rows["Zorbik Flux Widget"]
    assert unknown["status"] == "unmatched" and unknown["include"] is False and unknown["product_id"] is None
    assert body["summary"] == {"matched": 2, "check": 0, "unmatched": 1, "invalid": 0}


def test_plain_csv_is_detected_and_rate_comes_from_amount(owner_a, ids):
    res = preview(owner_a, "sales.csv", plain_csv((day(1), "Thread seal tape Astral", 4, "B-9", 156)))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["columns"] == {"date": "Bill Date", "item": "Item", "quantity": "Qty", "bill": "Bill No",
                               "amount": "Amount"}
    (row,) = body["rows"]
    assert row["product_id"] == ids["tape"] and row["include"] is True  # fuzzy match on the shop's own listing
    assert row["unit_price"] == 39 and row["bill"] == "B-9"


def test_detection_and_date_reading_units():
    detect = importer._detect_columns
    assert detect(["Voucher Date", "Particulars", "Sold Qty", "Vch No.", "Amount"], importer.SALES_COLUMN_ALIASES) == {
        "date": "Voucher Date", "item": "Particulars", "quantity": "Sold Qty", "bill": "Vch No.", "amount": "Amount"}
    # A tax column is never the price or the amount, even when it comes first or no exact name is present.
    assert detect(["Date", "Invoice No", "Item Name", "Quantity", "GST Rate", "Price/Unit", "Taxable Value", "Amount"],
                  importer.SALES_COLUMN_ALIASES) == {"date": "Date", "item": "Item Name", "quantity": "Quantity",
                                                     "bill": "Invoice No", "rate": "Price/Unit", "amount": "Amount"}
    assert detect(["Bill Date", "Item", "Qty", "Tax Rate", "Disc %", "Taxable Value", "Net Value"],
                  importer.SALES_COLUMN_ALIASES) == {"date": "Bill Date", "item": "Item", "quantity": "Qty",
                                                     "amount": "Net Value"}
    assert detect(["Item name", "GST Rate", "Opening Qty", "Closing Qty"]) == {"name": "Item name",
                                                                               "quantity": "Closing Qty"}
    parse = importer._parse_date
    assert parse("2026-04-01 00:00:00") == parse("01/04/2026") == parse("1-Apr-2026") == parse("01-04-26") \
        == parse("1 April 2026") == parse("46113") == date(2026, 4, 1)
    assert parse("Grand Total") is None and parse("31/02/2026") is None and parse(None) is None


def test_a_report_without_the_required_columns_is_refused(owner_a):
    res = preview(owner_a, "items.csv", "Item name,Closing Qty\nTape,3\n")
    assert res.status_code == 400 and res.json()["error"]["code"] == "no_sales_columns"


def test_returns_and_future_dates_are_invalid(owner_a):
    res = preview(owner_a, "sales.csv", plain_csv((day(1), TAPE, -2, "CN-1", -80), (day(-2), TAPE, 1, "B-1", 40),
                                                  (day(1), TAPE, 1, "B-2", 40)))
    rows = res.json()["rows"]
    assert [r["status"] for r in rows] == ["invalid", "invalid", "matched"]
    assert [r["include"] for r in rows] == [False, False, True]
    assert "return" in rows[0]["issues"][0] and "future" in rows[1]["issues"][0]
    assert res.json()["end"] == day(1).isoformat()  # an invalid row's date is not part of the period


# ---------------------------------------------------------------- preview and commit


def test_preview_then_commit_writes_import_rows_with_real_baskets(owner_a, db, ids):
    body = preview(owner_a, "sales-register.xlsx", tally_xlsx()).json()
    done = commit(owner_a, kept(body))
    assert done.status_code == 200, done.text
    assert done.json() == {"imported": 3, "lines": 3, "replaced": 0, "start": day(6).isoformat(),
                           "end": day(2).isoformat()}
    rows = db.sales_history.find({"shop_id": ids["shop_a"], "source": SaleSource.IMPORT.value})
    assert len(rows) == 3
    assert all(r.is_demo is False and r.customer_id is None and r.source == SaleSource.IMPORT for r in rows)
    bill_101 = [r for r in rows if r.sold_at == datetime.combine(day(6), datetime.min.time())]
    assert {r.product_id for r in bill_101} == {ids["charger"], ids["tape"]}
    assert len({r.basket_id for r in bill_101}) == 1 and "bill:101" in bill_101[0].basket_id  # one bill, one basket
    assert {r.basket_id for r in rows} != {bill_101[0].basket_id}
    tape = next(r for r in bill_101 if r.product_id == ids["tape"])
    assert tape.quantity == 3 and tape.unit_price == 40 and tape.category_id is not None


def test_lines_of_one_listing_on_one_bill_are_added_together(owner_a, db, ids):
    csv = plain_csv((day(2), TAPE, 2, "B-1", 80), (day(2), TAPE, 1, "B-1", 50), (day(2), TAPE, 1, "", 40),
                    (day(2), TAPE, 5, "", 200))
    done = commit(owner_a, kept(preview(owner_a, "sales.csv", csv).json()))
    assert done.status_code == 200, done.text
    rows = sorted(db.sales_history.find({"source": SaleSource.IMPORT.value}), key=lambda r: r.basket_id)
    assert [(r.quantity, r.unit_price) for r in rows] == [(3, 43.33), (6, 40.0)]
    assert rows[0].basket_id.endswith(":bill:B-1") and rows[1].basket_id.endswith(f":item:{ids['tape']}")


def test_another_shops_product_is_refused(owner_b, ids):
    res = commit(owner_b, {"start": day(2).isoformat(), "end": day(2).isoformat(), "rows": [
        {"product_id": ids["charger"], "date": day(2).isoformat(), "quantity": 1, "unit_price": 10}]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "not_your_product"


def test_the_stock_import_is_unchanged_by_the_sales_kind(owner_a):
    # A stock commit body (no kind) still goes to the stock import, and validates as before.
    res = owner_a.post("/api/owner/import/commit", json={"rows": []}, headers=HEADERS)
    assert res.status_code == 422
    res = owner_a.post("/api/owner/import/preview", headers=HEADERS,
                       files={"file": ("stock.csv", f"Particulars,Closing Qty\n{TAPE},5\n", "text/csv")})
    assert res.status_code == 200 and "summary" in res.json() and "kind" not in res.json()


# ---------------------------------------------------------------- re-upload replaces


def test_reupload_replaces_rather_than_duplicates(owner_a, db, ids):
    first = plain_csv((day(10), TAPE, 2, "B-1", 80), (day(8), TAPE, 1, "B-2", 40), (day(6), CHARGER, 1, "B-3", 1249))
    assert commit(owner_a, kept(preview(owner_a, "sales.csv", first).json())).json()["imported"] == 3
    again = preview(owner_a, "sales.csv", first).json()
    assert again["replaces"] == 3
    assert commit(owner_a, kept(again)).json() == {"imported": 3, "lines": 3, "replaced": 3,
                                                    "start": day(10).isoformat(), "end": day(6).isoformat()}
    assert db.sales_history.count({"source": SaleSource.IMPORT.value}) == 3

    # An overlapping period (day 8 to day 4) replaces the rows on days 8 and 6 and keeps day 10's.
    overlap = plain_csv((day(8), TAPE, 7, "B-2", 280), (day(4), CHARGER, 2, "B-9", 2498))
    assert commit(owner_a, kept(preview(owner_a, "sales.csv", overlap).json())).json()["replaced"] == 2
    rows = {(r.sold_at.date(), r.product_id): r.quantity for r in db.sales_history.find({"source": "import"})}
    assert rows == {(day(10), ids["tape"]): 2, (day(8), ids["tape"]): 7, (day(4), ids["charger"]): 2}


def test_the_unique_key_refuses_a_duplicate_import_row(owner_a, db):
    commit(owner_a, kept(preview(owner_a, "sales.csv", plain_csv((day(3), TAPE, 1, "B-1", 40))).json()))
    raw = db.sales_history.raw.find_one({"source": "import"}, {"_id": 0})
    with pytest.raises(DuplicateKeyError):
        db.sales_history.raw.insert_one({**raw, "_id": 10_000_000})
    # Rows from reservations and orders are not under the key.
    db.sales_history.raw.insert_many([{**raw, "_id": 10_000_001 + n, "source": "reservation"} for n in range(2)])
    db.sales_history.raw.delete_many({"_id": {"$gte": 10_000_000}})


def test_another_shops_imports_are_not_replaced(owner_a, owner_b, db, ids):
    commit(owner_a, kept(preview(owner_a, "sales.csv", plain_csv((day(3), TAPE, 1, "B-1", 40))).json()))
    res = commit(owner_b, {"start": day(3).isoformat(), "end": day(3).isoformat(), "rows": [
        {"product_id": ids["tape_far"], "date": day(3).isoformat(), "quantity": 2, "unit_price": 38}]})
    assert res.json()["replaced"] == 0
    assert db.sales_history.count({"source": "import", "shop_id": ids["shop_a"]}) == 1


# ---------------------------------------------------------------- the 90-day limit


def test_an_upload_covers_at_most_90_days(owner_a):
    ok = preview(owner_a, "sales.csv", plain_csv((day(90), TAPE, 1, "B-1", 40), (day(1), TAPE, 1, "B-2", 40)))
    assert ok.status_code == 200, ok.text  # 90 days, first to last inclusive
    too_long = preview(owner_a, "sales.csv", plain_csv((day(91), TAPE, 1, "B-1", 40), (day(1), TAPE, 1, "B-2", 40)))
    assert too_long.status_code == 400 and too_long.json()["error"]["code"] == "range_too_long"

    row = {"product_id": 0, "date": day(1).isoformat(), "quantity": 1, "unit_price": 40}
    res = commit(owner_a, {"start": day(91).isoformat(), "end": day(1).isoformat(), "rows": [row]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "range_too_long"
    res = commit(owner_a, {"start": day(1).isoformat(), "end": day(-1).isoformat(), "rows": [row]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "future_date"


def test_rows_outside_the_period_are_refused(owner_a, ids):
    res = commit(owner_a, {"start": day(3).isoformat(), "end": day(2).isoformat(), "rows": [
        {"product_id": ids["tape"], "date": day(5).isoformat(), "quantity": 1, "unit_price": 40}]})
    assert res.status_code == 400 and res.json()["error"]["code"] == "bad_range"


def test_the_row_limit_is_the_stock_imports(owner_a, monkeypatch):
    monkeypatch.setattr(importer.settings, "import_max_rows", 3)
    csv = plain_csv(*[(day(1), TAPE, 1, f"B-{n}", 40) for n in range(4)])
    res = preview(owner_a, "sales.csv", csv)
    assert res.status_code == 400 and res.json()["error"]["code"] == "too_many_rows"


# ---------------------------------------------------------------- never demo


def test_imported_rows_are_never_marked_demo(owner_a, db, ids):
    body = kept(preview(owner_a, "sales.csv", plain_csv((day(2), TAPE, 1, "B-1", 40))).json())
    body["is_demo"] = True
    body["source"] = "demo"
    body["rows"] = [{**r, "is_demo": True, "source": "demo", "customer_id": 3} for r in body["rows"]]
    assert commit(owner_a, body).status_code == 200
    rows = db.sales_history.raw.find({"source": {"$in": ["import", "demo"]}, "shop_id": ids["shop_a"]})
    assert [(r["source"], r["is_demo"], r["customer_id"]) for r in rows] == [("import", False, None)]


# ---------------------------------------------------------------- the migration


def test_migration_adds_the_enum_value_and_unique_key(scratch):
    migrate.run(scratch)  # a new database: the baseline already has both, and migration 10 changes nothing
    info = scratch["sales_history"].index_information()
    assert info["uq_import_sale"]["unique"] is True
    assert info["uq_import_sale"]["partialFilterExpression"] == {"source": "import"}

    # A database from before P7.2: no import value in the validator, no unique key.
    validator = next(scratch.list_collections(filter={"name": "sales_history"}))["options"]["validator"]
    validator["$jsonSchema"]["properties"]["source"]["enum"].remove("import")
    scratch.command("collMod", "sales_history", validator=validator)
    scratch["sales_history"].drop_index("uq_import_sale")

    for _ in range(2):  # idempotent
        m0010_sales_import.apply(scratch)
        validator = next(scratch.list_collections(filter={"name": "sales_history"}))["options"]["validator"]
        assert validator["$jsonSchema"]["properties"]["source"]["enum"].count("import") == 1
        assert "uq_import_sale" in scratch["sales_history"].index_information()
