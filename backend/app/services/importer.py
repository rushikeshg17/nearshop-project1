"""Bulk stock import from the Excel/CSV item list a shop already has.

Most local shops keep stock in a billing app (Vyapar, Tally, Marg, Khatabook...) that can export
an item list. The owner uploads that file; we detect the columns, match every row to their existing
listings or to the NearShop catalogue with fuzzy matching, and show a preview. Nothing changes
until the owner confirms (commit).

The same apps export a sales report (bill-wise or item-wise sales register). sales_preview and sales_commit
import it into sales_history with source import (P7.2), so the demand and bundle models see a shop's real sales
rather than only the few that pass through NearShop reservations and orders.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz, process

from app.core import cache_version
from app.core.config import settings
from app.core.database import Database
from app.core.errors import AppError
from app.models import CatalogItem, Product, SaleSource, SalesRecord, Shop
from app.services.inventory import apply_bulk
from app.services.loaders import categories as load_categories
from app.services.loaders import with_category

if TYPE_CHECKING:  # pandas is imported on use: it stays out of the API's serving memory
    import pandas as pd

# The commit route's row cap, fixed when the request model is defined; reading uses the live settings.
MAX_ROWS = settings.import_max_rows
AUTO_MATCH = 86  # at or above: confidently the same item
SUGGEST_MATCH = 72  # between SUGGEST and AUTO: suggested, owner should check

# Header names used by common billing apps and hand-made sheets.
COLUMN_ALIASES: dict[str, list[str]] = {
    "name": ["item name", "item", "product name", "product", "name", "particulars", "item description", "description",
             "stock item", "item details"],
    "quantity": ["quantity", "qty", "stock", "closing stock", "closing qty", "current stock", "stock qty", "balance",
                 "in stock", "available", "available qty", "opening stock"],
    "price": ["selling price", "sale price", "sales price", "price", "rate", "sp", "unit price", "retail price"],
    "mrp": ["mrp", "m.r.p", "max retail price"],
    "brand": ["brand", "company", "manufacturer", "make"],
    "category": ["category", "item group", "group", "department", "type", "stock group"],
    "sku": ["sku", "item code", "code", "barcode", "product code"],
}

TEMPLATE_CSV = (
    "Item name,Brand,Category,Quantity,Selling price,MRP\n"
    "Samsung 25W USB-C Fast Charger,Samsung,Mobile Accessories,6,1249,1699\n"
    "Astral PTFE Thread Seal Tape 12 mm,Astral,Plumbing,40,40,50\n"
    "Classmate Long Notebook 172 pages,Classmate,Stationery,25,65,70\n"
)


# Sales reports (P7.2): the columns a billing app's sales register uses. date, item and quantity are required;
# bill and either rate or amount are optional. Fields are detected in this order, each taking a column once.
SALES_COLUMN_ALIASES: dict[str, list[str]] = {
    "date": ["date", "voucher date", "bill date", "invoice date", "sale date", "txn date", "vch date"],
    "item": ["particulars", "item name", "item", "product name", "product", "stock item", "item description",
             "description"],
    "quantity": ["qty", "quantity", "sold qty", "sale qty", "sales qty", "billed qty", "qty sold", "units"],
    "bill": ["bill no", "bill number", "bill no.", "invoice no", "invoice number", "inv no", "voucher no",
             "vch no", "voucher number", "receipt no", "bill #", "invoice #", "invoice"],
    "rate": ["rate", "price unit", "price per unit", "unit price", "sale price", "selling price", "price"],
    "amount": ["amount", "value", "net amount", "total amount", "line total", "total", "gross amount"],
}
SALES_REQUIRED = ("date", "item", "quantity")
SALES_MAX_DAYS = 90  # one upload covers at most this many days, first to last date inclusive
HEADER_SCAN_ROWS = 10  # a report's title lines (shop name, period) above the header row are skipped
IST = ZoneInfo("Asia/Kolkata")
BILL_MAX_LENGTH = 60
# Day-first, as Indian billing apps write dates; ISO dates (and the date cells of a .xlsx) are read first.
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%y", "%d/%m/%y", "%d.%m.%y", "%d-%b-%Y", "%d-%b-%y",
                "%d %b %Y", "%d %b %y", "%d/%b/%Y", "%d-%B-%Y", "%d %B %Y", "%b %d, %Y", "%d %b, %Y")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$")
_TRAILING_TIME = re.compile(r"\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?$", re.IGNORECASE)
_EXCEL_EPOCH = date(1899, 12, 30)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9. ]+", " ", str(s).lower()).strip()


# A header naming a tax or discount (GST Rate, Tax Amount, Taxable Value, Disc %) is never a price, amount,
# quantity or item column, however its other words read: billing reports, Indian ones especially, carry them.
_TAX_WORDS = ("gst", "cgst", "sgst", "igst", "utgst", "tax", "vat", "cess", "disc")


def _is_tax_column(normed: str) -> bool:
    return any(word.startswith(_TAX_WORDS) for word in normed.split())


def _detect_columns(columns: list[str], aliases_by_field: dict[str, list[str]] = COLUMN_ALIASES) -> dict[str, str]:
    """Map each field to a column. Exact header names are matched first, for every field, and only then
    headers that merely contain an alias: so an exact "Price/Unit" or "Amount" is never lost to a "GST Rate"
    or "Taxable Value" that happens to contain "rate" or "value". Tax and discount columns never match by
    containment. Within each pass, a field's aliases are tried in order of preference."""
    found: dict[str, str] = {}
    normed = {c: _norm(c) for c in columns}

    def claim(field: str, aliases: list[str], exact: bool) -> None:
        for alias in aliases:  # aliases are ordered by preference
            hit = next((c for c, n in normed.items() if c not in found.values() and (
                n == alias if exact else alias in n and not _is_tax_column(n))), None)
            if hit is not None:
                found[field] = hit
                return

    for exact in (True, False):
        for field, aliases in aliases_by_field.items():
            if field not in found:
                claim(field, aliases, exact)
    return {field: found[field] for field in aliases_by_field if field in found}


def _number(v) -> float | None:
    if v is None or (isinstance(v, float) and v != v):  # NaN is the only value not equal to itself
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _check_xlsx_archive(data: bytes) -> None:
    """Refuse a workbook that would expand too far, from the archive's entry list alone, before any XML
    is parsed. Python's zipfile never inflates an entry past its declared size (it raises on a mismatch),
    so the declared sizes bound what openpyxl can read."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise AppError("Couldn't read that file. Check it opens in Excel and has a header row.", code="bad_file") from None
    limit_mb = settings.import_max_uncompressed_mb
    total = sum(e.file_size for e in entries)
    too_dense = any(e.file_size > settings.import_max_compression_ratio * max(e.compress_size, 1) for e in entries)
    if total > limit_mb * 1024 * 1024 or too_dense:
        raise AppError(f"That workbook expands to too much data (the limit is {limit_mb} MB unpacked). Export only "
                       "the item list, or save it as .csv.", code="file_too_large")


def _xlsx_rows(data: bytes) -> Iterator[list]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
    try:
        ws = wb.worksheets[0]  # the first sheet, as before
        ws.reset_dimensions()  # never pad rows to the width or height the file claims
        for row in ws.iter_rows(values_only=True):
            yield [None if v is None else str(v) for v in row]
    finally:
        wb.close()


def _csv_rows(data: bytes) -> Iterator[list]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    yield from csv.reader(io.StringIO(text, newline=""))


def _bounded_table(rows: Iterator[list]) -> tuple[list[str], list[list], list[int]]:
    """The header and data rows, read one row at a time and stopped at the first row past a limit: more
    than settings.import_max_rows non-blank data rows, or a row with a value beyond column
    settings.import_max_columns. Blank rows are skipped, and the first non-blank row is the header. The third
    value is each data row's line number in the file (1 is the first line), for the messages that name a row."""
    max_rows, max_cols = settings.import_max_rows, settings.import_max_columns
    header: list | None = None
    body: list[list] = []
    lines: list[int] = []
    for line, raw in enumerate(rows, start=1):
        width = len(raw)
        while width and (raw[width - 1] is None or str(raw[width - 1]).strip() == ""):
            width -= 1
        if not width:
            continue
        if width > max_cols:
            raise AppError(f"That file has more than {max_cols} columns; keep only the item list columns.",
                           code="too_many_columns")
        if header is None:
            header = raw[:width]
            continue
        if len(body) >= max_rows:
            raise AppError(f"That file has more than {max_rows} rows; the limit is {max_rows} per upload.",
                           code="too_many_rows")
        body.append(raw[:width])
        lines.append(line)
    if header is None:
        raise AppError("Couldn't read that file. Check it opens in Excel and has a header row.", code="bad_file")
    width = max([len(header), *(len(r) for r in body)])
    return _column_names(header, width), [r + [None] * (width - len(r)) for r in body], lines


def _column_names(header: list, width: int) -> list[str]:
    names: list[str] = []
    for i in range(width):
        name = str(header[i]).strip() if i < len(header) and header[i] is not None else ""
        name = name or f"Unnamed: {i}"
        base, n = name, 0
        while name in names:  # duplicate headers get a suffix, as pandas gave them
            n += 1
            name = f"{base}.{n}"
        names.append(name)
    return names


def read_table(data: bytes, filename: str) -> pd.DataFrame:
    """Parse an uploaded .xlsx or .csv item list within the limits in settings (plan section 8.13), all
    enforced while reading: a .xlsx archive is checked before it is parsed, and reading stops at the first
    row past the row or column limit. Blocking: runs in a worker thread (the import route is a def handler)."""
    import pandas as pd

    if len(data) > settings.import_max_mb * 1024 * 1024:
        raise AppError(f"File is larger than {settings.import_max_mb} MB. Split it or export only items in stock.",
                       code="file_too_large")
    name = filename.lower()
    if name.endswith(".xlsx"):
        _check_xlsx_archive(data)
        rows = _xlsx_rows(data)
    elif name.endswith(".csv") or name.endswith(".txt"):
        rows = _csv_rows(data)
    else:
        raise AppError("Upload a .xlsx or .csv file. Old .xls files: open and 'Save as' .xlsx first.", code="bad_file")
    try:
        columns, body, lines = _bounded_table(rows)
    except AppError:
        raise
    except Exception:
        raise AppError("Couldn't read that file. Check it opens in Excel and has a header row.", code="bad_file") from None
    finally:
        close = getattr(rows, "close", None)
        if close:
            close()  # stops the generator early, closing the workbook
    df = pd.DataFrame(body, columns=columns, dtype=object)
    df.attrs["lines"] = lines
    return df


def preview(db: Database, shop: Shop, data: bytes, filename: str) -> dict:
    df = read_table(data, filename)
    cols = _detect_columns([str(c) for c in df.columns])
    if "name" not in cols:
        raise AppError("Couldn't find an item name column. Name it 'Item name' (see the template).", code="no_name_column")

    matcher = _Matcher(db, shop)
    mine_by_name, mine_by_catalog = matcher.mine_by_name, matcher.mine_by_catalog
    categories = list(load_categories(db).values())
    cat_names = {c.slug: _norm(c.name) for c in categories}
    default_cat = shop.categories[0].slug if shop.categories else categories[0].slug

    rows = []
    for i, rec in enumerate(df.to_dict("records"), start=2):  # row 1 is the header in Excel
        name = str(rec.get(cols["name"]) or "").strip()
        if not name or name.lower() == "nan":
            continue
        qty = _number(rec.get(cols.get("quantity"))) if "quantity" in cols else None
        price = _number(rec.get(cols.get("price"))) if "price" in cols else None
        mrp = _number(rec.get(cols.get("mrp"))) if "mrp" in cols else None
        brand = str(rec.get(cols["brand"]) or "").strip() if "brand" in cols else ""
        brand = "" if brand.lower() == "nan" else brand
        price_from_mrp = price is None and mrp is not None
        if price_from_mrp:
            price = mrp

        row = {"row": i, "name": name, "brand": brand or None, "quantity": int(qty) if qty is not None and qty >= 0 else None,
               "price": round(price, 2) if price else None, "mrp": round(mrp, 2) if mrp else None,
               "action": "add_new", "product_id": None, "catalog_item_id": None, "match_name": None, "score": 0,
               "category_slug": default_cat, "issues": []}

        existing = mine_by_name.get(_norm(name))
        match = matcher.catalog_match(name, brand)
        if match:
            item, score = match
            cid = item.id
            row["category_slug"] = item.category.slug if score >= SUGGEST_MATCH - 10 else row["category_slug"]
            if score >= SUGGEST_MATCH:
                row.update(catalog_item_id=cid, match_name=item.name, score=round(score))
                if existing is None and cid in mine_by_catalog:
                    existing = mine_by_catalog[cid]
                if existing is None:
                    row["action"] = "add_catalog"
                    if score < AUTO_MATCH:
                        row["issues"].append("Check the catalogue match")
                    if row["price"] is None and item.typical_price:
                        row["price"] = item.typical_price
                        row["issues"].append("No price in file; using the typical local price")
        if existing is not None:
            row.update(action="update", product_id=existing.id, match_name=existing.name,
                       score=max(row["score"], 100 if _norm(existing.name) == _norm(name) else row["score"]))
            if row["price"] is None:
                row["price"] = existing.price

        if "category" in cols and row["action"] == "add_new":
            raw = _norm(rec.get(cols["category"]) or "")
            if raw:
                best = process.extractOne(raw, cat_names, scorer=fuzz.WRatio)
                if best and best[1] >= 70:
                    row["category_slug"] = best[2]
        if price_from_mrp:
            row["issues"].append("No selling price in file; using MRP")
        if row["quantity"] is None:
            row["issues"].append("No quantity; stock will not be changed" if row["action"] == "update" else "No quantity; will be added as 0")
        if row["price"] is None and row["action"] != "update":
            row["issues"].append("Price missing: enter one before importing")
        rows.append(row)

    summary = {a: sum(1 for r in rows if r["action"] == a) for a in ("update", "add_catalog", "add_new")}
    return {"columns": cols, "rows": rows, "summary": summary, "total": len(rows)}


class _Matcher:
    """Fuzzy matching of a row's item name to the shop's own listings and to the NearShop catalogue, shared by the
    stock and sales imports."""

    def __init__(self, db: Database, shop: Shop) -> None:
        catalog, mine = db.gather(lambda: with_category(db, db.catalog_items.find()),
                                  lambda: db.products.find({"shop_id": shop.id}))
        self.catalog_keys = {c.id: _norm(f"{c.brand or ''} {c.name}") for c in catalog}
        self.catalog_by_id = {c.id: c for c in catalog}
        self.mine_by_id = {p.id: p for p in mine}
        self.mine_by_name = {_norm(p.name): p for p in mine}
        self.mine_by_catalog = {p.catalog_item_id: p for p in mine if p.catalog_item_id}
        self.mine_keys = {p.id: _norm(f"{p.brand or ''} {p.name}") for p in mine}

    @staticmethod
    def _cap(name: str, score: float) -> float:
        # token_set_ratio scores 100 when one name's words are a subset of the other's, so a bare
        # "cable" would match every cable. Require at least two meaningful words to auto-match.
        if len([w for w in _norm(name).split() if len(w) > 2]) < 2:
            return min(score, SUGGEST_MATCH - 1)
        return score

    def catalog_match(self, name: str, brand: str = "") -> tuple[CatalogItem, float] | None:
        """The closest catalogue item and its score (0 to 100), or None for an empty catalogue."""
        match = process.extractOne(_norm(f"{brand} {name}"), self.catalog_keys, scorer=fuzz.token_set_ratio)
        if not match:
            return None
        _, score, cid = match
        return self.catalog_by_id[cid], self._cap(name, score)

    def listing_match(self, name: str) -> tuple[Product, float] | None:
        """The shop's listing a sales line is for, and the match score: an exact name scores 100; otherwise the
        better of a fuzzy match on the shop's own listing names and a catalogue match the shop lists. None when
        nothing reaches SUGGEST_MATCH."""
        exact = self.mine_by_name.get(_norm(name))
        if exact is not None:
            return exact, 100
        best: tuple[Product, float] | None = None
        own = process.extractOne(_norm(name), self.mine_keys, scorer=fuzz.token_set_ratio)
        if own:
            _, score, pid = own
            best = (self.mine_by_id[pid], self._cap(name, score))
        cat = self.catalog_match(name)
        if cat and cat[0].id in self.mine_by_catalog and (best is None or cat[1] > best[1]):
            best = (self.mine_by_catalog[cat[0].id], cat[1])
        return best if best and best[1] >= SUGGEST_MATCH else None


def commit(db: Database, shop: Shop, rows: list[dict], actor_id: int) -> dict:
    """Apply confirmed rows in a single transaction: all rows succeed or none do. Then bump the shared listings
    version, so every API process's search sees the changes.

    Everything is validated and prepared in memory first, then written in a few bulk operations,
    so a 2,000-row sheet is as safe (and nearly as fast) as a 2-row one."""
    counts = {"updated": 0, "added": 0, "skipped": 0}
    cats = {c.slug: c for c in load_categories(db).values()}
    cats_by_id = load_categories(db)
    mine = db.products.find({"shop_id": shop.id})
    mine_by_id = {p.id: p for p in mine}
    mine_by_catalog = {p.catalog_item_id: p for p in mine if p.catalog_item_id}
    catalog = db.catalog_items.by_ids(r.get("catalog_item_id") for r in rows if r["action"] == "add_catalog")

    creates: list[tuple[Product, int]] = []
    updates: dict[int, tuple[Product, dict, int | None]] = {}

    def update(p: Product, price, qty) -> None:
        _, fields, old_qty = updates.get(id(p), (p, {}, None))
        fields["is_active"] = True
        if price:
            fields["price"] = float(price)
        updates[id(p)] = (p, fields, int(qty) if qty is not None else old_qty)
        counts["updated"] += 1

    for r in rows:
        action = r["action"]
        qty = r.get("quantity")
        price = r.get("price")
        if action == "skip":
            counts["skipped"] += 1
            continue
        if action == "update":
            p = mine_by_id.get(r["product_id"])
            if p is None:
                raise AppError(f"Row {r.get('row')}: that product is not in your shop")
            update(p, price, qty)
            continue
        if not price or float(price) <= 0:
            raise AppError(f"Row {r.get('row')} ({r.get('name')}): enter a price", code="missing_price")
        if action == "add_catalog":
            c = catalog.get(r["catalog_item_id"])
            if c is None:
                raise AppError(f"Row {r.get('row')}: catalogue item not found")
            existing = mine_by_catalog.get(c.id)
            if existing:  # listed meanwhile (or duplicate row): treat as update
                update(existing, price, qty)
                continue
            category = cats_by_id[c.category_id]
            p = Product(shop_id=shop.id, catalog_item_id=c.id, category_id=c.category_id, name=c.name,
                        brand=c.brand, unit=c.unit, description=c.description, specs=c.specs or {},
                        keywords=" ".join(dict.fromkeys([*(c.tags or []), category.name.lower(),
                                                         (c.subcategory or "").replace("-", " ")])),
                        icon=c.icon, price=float(price), mrp=c.mrp, quantity=0)
            mine_by_catalog[c.id] = p
        elif action == "add_new":
            cat = cats.get(r.get("category_slug") or "")
            if cat is None:
                raise AppError(f"Row {r.get('row')} ({r.get('name')}): choose a category")
            name = str(r.get("name") or "").strip()[:200]
            if len(name) < 2:
                raise AppError(f"Row {r.get('row')}: item name is missing")
            p = Product(shop_id=shop.id, category_id=cat.id, name=name, brand=(r.get("brand") or None),
                        keywords=cat.name.lower(), icon=cat.icon, price=float(price),
                        mrp=float(r["mrp"]) if r.get("mrp") else None, quantity=0)
        else:
            raise AppError(f"Unknown action '{action}'")
        creates.append((p, int(qty or 0)))
        counts["added"] += 1

    # A duplicate catalogue row that followed its own "add" in the same file updates the new listing.
    new_ids = {id(p) for p, _ in creates}
    for key, (p, fields, qty) in list(updates.items()):
        if id(p) in new_ids:
            for k, v in fields.items():
                setattr(p, k, v)
            if qty is not None:
                creates = [(cp, qty if cp is p else cq) for cp, cq in creates]
            del updates[key]
    apply_bulk(db, shop, actor_id, creates, list(updates.values()))
    # Committed: every API process's search caches follow within 5 seconds (P8.1).
    cache_version.bump(db.mongo, cache_version.LISTINGS)
    return counts


# ---------------------------------------------------------------- sales reports (P7.2)


def _parse_date(v) -> date | None:
    """A sale date from a report cell: ISO (and .xlsx date cells), day-first Indian formats, or an Excel day
    serial number. None when it is not a date."""
    if v is None:
        return None
    s = _TRAILING_TIME.sub("", str(v).strip())
    if not s or s.lower() == "nan":
        return None
    try:
        if m := _ISO_DATE.match(s):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if re.fullmatch(r"\d{5}(?:\.0+)?", s):  # a date cell exported as its serial number
            return _EXCEL_EPOCH + timedelta(days=int(float(s)))
    except ValueError:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _today() -> date:
    return datetime.now(IST).date()


def _sold_at(d: date) -> datetime:
    """The sold_at stored for a report date: 00:00 UTC that day (05:30 in India), which falls on the same calendar
    date whether a reader groups by UTC or by India time. Naive UTC, as every stored date."""
    return datetime.combine(d, time())


def _basket_id(shop_id: int, d: date, bill: str | None, product_id: int) -> str:
    """Lines of one bill share a basket, which gives Apriori real baskets. A line without a bill number is a basket
    of its own."""
    return f"import:{shop_id}:{d.isoformat()}:" + (f"bill:{bill}" if bill else f"item:{product_id}")


def _clean_bill(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d+\.0+", s):  # a bill number read from a numeric cell
        s = s.split(".", 1)[0]
    if s.lower() == "nan":
        return None
    return s[:BILL_MAX_LENGTH] or None


def _check_period(start: date, end: date) -> None:
    days = (end - start).days + 1
    if days > SALES_MAX_DAYS:
        raise AppError(f"That report covers {days} days ({start:%d %b %Y} to {end:%d %b %Y}). Upload at most "
                       f"{SALES_MAX_DAYS} days at a time: export the report month by month.", code="range_too_long")


def _sales_columns(df: pd.DataFrame) -> tuple[dict[str, str], pd.DataFrame, list[int]]:
    """The detected columns, the data rows and each data row's line number in the file. A report whose first
    lines are a title (the shop's name, the period) has its header further down: the first HEADER_SCAN_ROWS rows
    are searched for it."""
    import pandas as pd

    lines: list[int] = df.attrs.get("lines") or list(range(2, len(df) + 2))
    cols = _detect_columns([str(c) for c in df.columns], SALES_COLUMN_ALIASES)
    if all(f in cols for f in SALES_REQUIRED):
        return cols, df, lines
    for i, raw in enumerate(df.head(HEADER_SCAN_ROWS).itertuples(index=False, name=None)):
        names = _column_names(list(raw), len(raw))
        found = _detect_columns(names, SALES_COLUMN_ALIASES)
        if all(f in found for f in SALES_REQUIRED):
            return found, pd.DataFrame(df.iloc[i + 1:].values.tolist(), columns=names, dtype=object), lines[i + 1:]
    missing = [f for f in SALES_REQUIRED if f not in cols]
    raise AppError(f"Couldn't find the {' and '.join(missing)} column{'s' if len(missing) > 1 else ''} in that "
                   "report. A sales report needs a date (Date, Voucher Date or Bill Date), an item (Particulars or "
                   "Item Name) and a quantity (Qty, Quantity or Sold Qty).", code="no_sales_columns")


_TOTAL_LINE = re.compile(r"grand total.*|(sub ?|net |bill |voucher |day )?totals?")


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and v != v) or str(v).strip().lower() in ("", "nan")


def _imported_between(shop: Shop, start: date, end: date) -> dict:
    return {"shop_id": shop.id, "source": SaleSource.IMPORT.value,
            "sold_at": {"$gte": _sold_at(start), "$lt": _sold_at(end + timedelta(days=1))}}


def sales_preview(db: Database, shop: Shop, data: bytes, filename: str) -> dict:
    """Read a sales report and match each line to the shop's listings. Nothing is written.

    Each row has a status: matched (confidently one of the shop's listings), check (a likely listing the owner
    should confirm), unmatched (no listing: the item must be listed first) or invalid (no usable date or
    quantity, a return, or a future date). Matched and check rows are included by default.

    Tally's item-wise registers write the date and voucher number only on a bill's first line: a line whose date
    cell is blank takes the date of the line above, and its bill number too when that cell is also blank, so the
    bill's other lines stay in its basket. Total lines ("Grand Total", "Sub Total") are skipped."""
    df = read_table(data, filename)
    cols, df, line_numbers = _sales_columns(df)
    matcher = _Matcher(db, shop)
    today = _today()
    rows: list[dict] = []
    last_date: date | None = None
    last_bill: str | None = None
    for i, rec in zip(line_numbers, df.to_dict("records"), strict=True):
        name = str(rec.get(cols["item"]) or "").strip()
        if not name or name.lower() == "nan" or _TOTAL_LINE.fullmatch(_norm(name)):
            continue
        raw_date = rec.get(cols["date"])
        qty = _number(rec.get(cols["quantity"]))
        rate = _number(rec.get(cols["rate"])) if "rate" in cols else None
        amount = _number(rec.get(cols["amount"])) if "amount" in cols else None
        bill = _clean_bill(rec.get(cols["bill"])) if "bill" in cols else None
        carried = _blank(raw_date) and last_date is not None
        if carried:
            d = last_date  # a later line of the bill above
            bill = bill if bill is not None else last_bill
        else:
            d = _parse_date(raw_date)
            last_date, last_bill = d, bill
        if d is None and qty is None:
            continue  # a subtotal or footer line
        row = {"row": i, "date": d.isoformat() if d else None, "name": name[:200], "bill": bill,
               "quantity": None, "unit_price": None, "status": "invalid", "include": False,
               "product_id": None, "match_name": None, "score": 0, "issues": []}
        issues = row["issues"]
        problems = []  # any of these makes the row invalid
        if d is None:
            problems.append(f"Couldn't read the date '{str(raw_date or '').strip()[:30]}'")
        elif d > today:
            problems.append("The date is in the future")
        if qty is None:
            problems.append("No quantity")
        elif qty < 0:
            problems.append("A return or credit note: not imported")
        else:
            whole = round(qty)
            if whole < 1:
                problems.append("Quantity is less than 1")
            else:
                if whole != qty:
                    issues.append(f"Quantity {qty:g} rounded to {whole}")
                row["quantity"] = whole
        if rate is not None and rate >= 0:
            row["unit_price"] = round(rate, 2)
        elif amount is not None and amount >= 0 and qty:
            row["unit_price"] = round(amount / qty, 2)

        match = matcher.listing_match(name)
        if match:
            product, score = match
            row.update(product_id=product.id, match_name=product.name, score=round(score))
            if row["unit_price"] is None:
                row["unit_price"] = product.price
                issues.append("No rate or amount in file; using your listed price")
        issues[:0] = problems
        if not problems:
            if match is None:
                row["status"] = "unmatched"
                issues.append("Not in your listings: add it (stock import) and upload again")
            else:
                row["status"] = "matched" if match[1] >= AUTO_MATCH else "check"
                row["include"] = True
                if row["status"] == "check":
                    issues.append("Check the match")
        rows.append(row)

    dates = [date.fromisoformat(r["date"]) for r in rows if r["date"] and r["status"] != "invalid"]
    if not dates:
        raise AppError("No sales with a date and quantity found in that report.", code="no_sales_rows")
    start, end = min(dates), max(dates)
    _check_period(start, end)
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in ("matched", "check", "unmatched", "invalid")}
    replaces = db.sales_history.count(_imported_between(shop, start, end))
    return {"kind": "sales", "columns": cols, "rows": rows, "summary": summary, "total": len(rows),
            "start": start.isoformat(), "end": end.isoformat(), "replaces": replaces}


def sales_commit(db: Database, shop: Shop, start: date, end: date, rows: list[dict]) -> dict:
    """Write the confirmed sales lines to sales_history with source import, replacing the shop's imported rows for
    every date from start to end (a re-upload of an overlapping period replaces, never duplicates). One
    transaction: all rows or none. Lines for the same listing, date and bill are added together."""
    if start > end:
        raise AppError("The report's first date is after its last date.", code="bad_range")
    _check_period(start, end)
    if end > _today():
        raise AppError("The report includes a future date.", code="future_date")
    products = db.products.by_ids(r["product_id"] for r in rows)
    lines: dict[tuple[int, date, str], list[float]] = defaultdict(lambda: [0, 0.0])
    for r in rows:
        p = products.get(r["product_id"])
        if p is None or p.shop_id != shop.id:
            raise AppError(f"Row {r.get('row')}: that product is not in your shop", code="not_your_product")
        d: date = r["date"]
        if not start <= d <= end:
            raise AppError(f"Row {r.get('row')}: {d:%d %b %Y} is outside the report's dates", code="bad_range")
        line = lines[(p.id, d, _basket_id(shop.id, d, _clean_bill(r.get("bill")), p.id))]
        line[0] += int(r["quantity"])
        line[1] += int(r["quantity"]) * float(r["unit_price"])
    records = [
        SalesRecord(shop_id=shop.id, product_id=pid, catalog_item_id=products[pid].catalog_item_id,
                    category_id=products[pid].category_id, customer_id=None, basket_id=basket,
                    quantity=qty, unit_price=round(total / qty, 2), source=SaleSource.IMPORT, is_demo=False,
                    sold_at=_sold_at(d))
        for (pid, d, basket), (qty, total) in lines.items()
    ]

    def write() -> int:
        replaced = db.sales_history.delete_many(_imported_between(shop, start, end))
        if records:
            db.sales_history.insert_many([r.model_copy() for r in records])
        return replaced

    replaced = db.transaction(write)
    return {"imported": len(records), "lines": len(rows), "replaced": replaced,
            "start": start.isoformat(), "end": end.isoformat()}
