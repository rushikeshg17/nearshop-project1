"""Bulk stock import from the Excel/CSV item list a shop already has.

Most local shops keep stock in a billing app (Vyapar, Tally, Marg, Khatabook...) that can export
an item list. The owner uploads that file; we detect the columns, match every row to their existing
listings or to the NearShop catalogue with fuzzy matching, and show a preview. Nothing changes
until the owner confirms (commit).
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, process

from app.core.config import settings
from app.core.database import Database
from app.core.errors import AppError
from app.models import Product, Shop
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


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9. ]+", " ", str(s).lower()).strip()


def normalize_name(s: str) -> str:
    """An item name as the matcher compares it: lower case, letters, digits, dots and single spaces."""
    return " ".join(_norm(s).split())


def capped_score(name: str, score: float) -> float:
    """token_set_ratio scores 100 when one name's words are a subset of the other's, so a bare "cable" would match
    every cable: a name with fewer than two meaningful words (longer than 2 letters) never scores a confident match."""
    if len([w for w in _norm(name).split() if len(w) > 2]) < 2:
        return min(score, SUGGEST_MATCH - 1)
    return score


def best_matches[K](query: str, choices: Mapping[K, str], limit: int = 1) -> list[tuple[K, float]]:
    """The matcher the import uses (and P4.4's stock replies on WhatsApp): token_set_ratio between the normalised
    query and each normalised choice, best first, as (key, raw score). Apply capped_score to the raw score before
    comparing it with AUTO_MATCH and SUGGEST_MATCH."""
    found = process.extract(_norm(query), choices, scorer=fuzz.token_set_ratio, limit=limit)
    return [(key, score) for _, score, key in found]


def _detect_columns(columns: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    normed = {c: _norm(c) for c in columns}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:  # aliases are ordered by preference
            hit = next((c for c, n in normed.items() if n == alias and c not in found.values()), None)
            if hit is None:
                hit = next((c for c, n in normed.items() if alias in n and c not in found.values()), None)
            if hit is not None:
                found[field] = hit
                break
    return found


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


def _bounded_table(rows: Iterator[list]) -> tuple[list[str], list[list]]:
    """The header and data rows, read one row at a time and stopped at the first row past a limit: more
    than settings.import_max_rows non-blank data rows, or a row with a value beyond column
    settings.import_max_columns. Blank rows are skipped, and the first non-blank row is the header."""
    max_rows, max_cols = settings.import_max_rows, settings.import_max_columns
    header: list | None = None
    body: list[list] = []
    for raw in rows:
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
    if header is None:
        raise AppError("Couldn't read that file. Check it opens in Excel and has a header row.", code="bad_file")
    width = max([len(header), *(len(r) for r in body)])
    names: list[str] = []
    for i in range(width):
        name = str(header[i]).strip() if i < len(header) and header[i] is not None else ""
        name = name or f"Unnamed: {i}"
        base, n = name, 0
        while name in names:  # duplicate headers get a suffix, as pandas gave them
            n += 1
            name = f"{base}.{n}"
        names.append(name)
    return names, [r + [None] * (width - len(r)) for r in body]


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
        columns, body = _bounded_table(rows)
    except AppError:
        raise
    except Exception:
        raise AppError("Couldn't read that file. Check it opens in Excel and has a header row.", code="bad_file") from None
    finally:
        close = getattr(rows, "close", None)
        if close:
            close()  # stops the generator early, closing the workbook
    return pd.DataFrame(body, columns=columns, dtype=object)


def preview(db: Database, shop: Shop, data: bytes, filename: str) -> dict:
    df = read_table(data, filename)
    cols = _detect_columns([str(c) for c in df.columns])
    if "name" not in cols:
        raise AppError("Couldn't find an item name column. Name it 'Item name' (see the template).", code="no_name_column")

    catalog, mine = db.gather(lambda: with_category(db, db.catalog_items.find()),
                              lambda: db.products.find({"shop_id": shop.id}))
    catalog_keys = {c.id: _norm(f"{c.brand or ''} {c.name}") for c in catalog}
    catalog_by_id = {c.id: c for c in catalog}
    mine_by_name = {_norm(p.name): p for p in mine}
    mine_by_catalog = {p.catalog_item_id: p for p in mine if p.catalog_item_id}
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
        match = best_matches(f"{brand} {name}", catalog_keys)
        if match:
            [(cid, score)] = match
            score = capped_score(name, score)  # at least two meaningful words to auto-match
            item = catalog_by_id[cid]
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


def commit(db: Database, shop: Shop, rows: list[dict], actor_id: int) -> dict:
    """Apply confirmed rows in a single transaction: all rows succeed or none do.

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
    return counts
