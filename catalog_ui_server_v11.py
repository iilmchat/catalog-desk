# -*- coding: utf-8 -*-
"""Локальный интерфейс каталога Пятёрочки (V11, Python stdlib + чистый JS).

Запуск: python catalog_ui_server_v10.py
Открывается только на 127.0.0.1, без внешних CDN и npm.
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

import sqlite3

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "ui_v11"
EXPORT_DIR = BASE_DIR / "5ka_exports"
CATEGORY_DIR = BASE_DIR / "5ka_category_lists"
DETAIL_DIR = BASE_DIR / "5ka_product_details"
PHOTO_DIR = BASE_DIR / "photos5ka"
DB_PATH = BASE_DIR / "pyaterochka_catalog_v9.sqlite3"
IMPORTER_PATH = BASE_DIR / "import_5ka_catalog_v9.py"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
EXPECTED_SAP = "35XY"

_JOB_LOCK = threading.Lock()
_JOB = {
    "running": False, "job_id": 0, "started_at": None, "finished_at": None,
    "return_code": None, "output": "Импорт ещё не запускался.", "error": None,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Не найдена {DB_PATH.name}. Сначала запустите V9-импортёр: "
            "python import_5ka_catalog_v9.py --no-photos"
        )
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_ui_columns() -> None:
    """Idempotent UI and enrichment schema migration; never drops existing V9 data."""
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(exports)")}
        if "max_page_linked" not in columns:
            conn.execute("ALTER TABLE exports ADD COLUMN max_page_linked INTEGER")
        if "visible_page_numbers" not in columns:
            conn.execute("ALTER TABLE exports ADD COLUMN visible_page_numbers TEXT")
        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_exports_category_page ON exports(sap_code, category, page_number);
        CREATE TABLE IF NOT EXISTS product_details (
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            product_name TEXT,
            product_url TEXT,
            image_url TEXT,
            description TEXT,
            composition TEXT,
            nutrition_json TEXT,
            attributes_json TEXT,
            details_text TEXT,
            first_imported_at TEXT NOT NULL,
            last_imported_at TEXT NOT NULL,
            last_captured_at TEXT NOT NULL,
            last_source_hash TEXT,
            PRIMARY KEY (sap_code, plu)
        );
        CREATE TABLE IF NOT EXISTS product_detail_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            file_name TEXT NOT NULL,
            details_json TEXT NOT NULL,
            UNIQUE(source_hash, sap_code, plu)
        );
        CREATE INDEX IF NOT EXISTS idx_product_detail_history_time ON product_detail_history(sap_code, plu, captured_at);
        CREATE TABLE IF NOT EXISTS product_detail_imports (
            source_hash TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            product_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS favorites (
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (sap_code, plu)
        );
        """)
        conn.commit()
    finally:
        conn.close()


def safe_json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def row_dict(row: sqlite3.Row | None):
    return dict(row) if row is not None else None


def get_price_changes(conn: sqlite3.Connection, limit: int = 500, direction: str = "all") -> list[dict]:
    # Compare consecutive observations for the same SAP+PLU.
    sql = """
    WITH ordered AS (
      SELECT h.*,
        LAG(current_price) OVER (PARTITION BY sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_current_price,
        LAG(regular_price) OVER (PARTITION BY sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_regular_price,
        LAG(promo_price) OVER (PARTITION BY sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_promo_price,
        LAG(discount_pct) OVER (PARTITION BY sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_discount_pct,
        LAG(promo_label) OVER (PARTITION BY sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_promo_label,
        LAG(captured_at) OVER (PARTITION BY sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_captured_at
      FROM price_history h
    ), changed AS (
      SELECT *,
        CASE WHEN current_price IS NOT prev_current_price THEN 1 ELSE 0 END AS c_price,
        CASE WHEN regular_price IS NOT prev_regular_price THEN 1 ELSE 0 END AS c_regular,
        CASE WHEN promo_price IS NOT prev_promo_price THEN 1 ELSE 0 END AS c_promo,
        CASE WHEN discount_pct IS NOT prev_discount_pct THEN 1 ELSE 0 END AS c_discount,
        CASE WHEN promo_label IS NOT prev_promo_label THEN 1 ELSE 0 END AS c_label
      FROM ordered WHERE prev_captured_at IS NOT NULL
    )
    SELECT * FROM changed
    WHERE c_price+c_regular+c_promo+c_discount+c_label > 0
    ORDER BY captured_at DESC, imported_at DESC, id DESC
    LIMIT ?
    """
    rows = [dict(r) for r in conn.execute(sql, (max(1, min(limit, 2000)),)).fetchall()]
    out = []
    for r in rows:
        oldp, newp = r.get("prev_current_price"), r.get("current_price")
        delta = round(float(newp) - float(oldp), 2) if oldp is not None and newp is not None else None
        pct = round(delta / float(oldp) * 100, 2) if delta is not None and oldp not in (None, 0) else None
        fields = []
        for key, label in (("c_price", "текущая цена"), ("c_regular", "обычная цена"),
                           ("c_promo", "акционная цена"), ("c_discount", "скидка"),
                           ("c_label", "условия акции")):
            if r[key]: fields.append(label)
        direction_label = "повышение" if delta is not None and delta > 0 else "снижение" if delta is not None and delta < 0 else "изменение условий"
        if direction == "up" and not (delta is not None and delta > 0):
            continue
        if direction == "down" and not (delta is not None and delta < 0):
            continue
        out.append({
            "captured_at": r["captured_at"], "imported_at": r["imported_at"],
            "previous_captured_at": r["prev_captured_at"], "sap_code": r["sap_code"], "plu": r["plu"],
            "name": r["product_name"], "category": r["category"],
            "old_price": oldp, "new_price": newp, "delta": delta, "delta_pct": pct,
            "old_regular": r["prev_regular_price"], "new_regular": r["regular_price"],
            "old_promo": r["prev_promo_price"], "new_promo": r["promo_price"],
            "old_discount": r["prev_discount_pct"], "new_discount": r["discount_pct"],
            "what_changed": ", ".join(fields), "direction": direction_label,
        })
    return out


def dashboard_data() -> dict:
    conn = connect_db()
    try:
        p = conn.execute("""SELECT COUNT(*) products,
            SUM(current_price IS NOT NULL) with_price,
            SUM(discount_pct IS NOT NULL) with_discount,
            SUM(image_url IS NOT NULL AND image_url!='') with_image_url,
            SUM(image_file IS NOT NULL AND image_file!='') with_local_image,
            COUNT(DISTINCT sap_code) sap_count FROM products""").fetchone()
        h = conn.execute("SELECT COUNT(*) observations, COUNT(DISTINCT source_hash) observed_exports FROM price_history").fetchone()
        ex = conn.execute("SELECT COUNT(*) exports, MAX(imported_at) last_import, MAX(captured_at) last_capture FROM exports").fetchone()
        cat = conn.execute("SELECT COUNT(DISTINCT category) FROM product_categories").fetchone()[0]
        distinct_pages = conn.execute("SELECT COUNT(DISTINCT sap_code||'|'||COALESCE(category,'')||'|'||COALESCE(page_number,'?')) FROM exports").fetchone()[0]
        changes = get_price_changes(conn, 1000)
        recent_exports = [dict(r) for r in conn.execute("""
            SELECT file_name,category,sap_code,page_number,product_rows,unique_products,captured_at,imported_at,source_url
            FROM exports ORDER BY imported_at DESC LIMIT 8
        """).fetchall()]
        by_category = category_progress(conn)
        new_since = conn.execute("SELECT COUNT(*) FROM products WHERE first_imported_at >= datetime('now','-7 days')").fetchone()[0]
        return {
            "sap": EXPECTED_SAP, "products": int(p["products"] or 0), "with_price": int(p["with_price"] or 0),
            "with_discount": int(p["with_discount"] or 0), "with_image_url": int(p["with_image_url"] or 0),
            "with_local_image": int(p["with_local_image"] or 0), "sap_count": int(p["sap_count"] or 0),
            "observations": int(h["observations"] or 0), "exports": int(ex["exports"] or 0),
            "last_import": ex["last_import"], "last_capture": ex["last_capture"],
            "categories": int(cat or 0), "pages": int(distinct_pages or 0),
            "changes_count": len(changes), "recent_changes": changes[:8],
            "recent_exports": recent_exports, "category_progress": by_category,
            "new_products_7d": int(new_since or 0),
            "with_details": int(conn.execute("SELECT COUNT(*) FROM product_details").fetchone()[0]),
            "favorites": int(conn.execute("SELECT COUNT(*) FROM favorites").fetchone()[0]),
        }
    finally:
        conn.close()


def category_progress(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
      SELECT e.sap_code, COALESCE(e.category,'Не указана') AS category,
             COUNT(*) AS files_count,
             COUNT(DISTINCT e.page_number) AS page_count,
             GROUP_CONCAT(DISTINCT e.page_number) AS pages_raw,
             MAX(e.max_page_linked) AS max_page_linked,
             MAX(e.captured_at) AS last_capture,
             MAX(e.imported_at) AS last_import,
             SUM(e.product_rows) AS rows_count,
             COUNT(DISTINCT p.plu) AS unique_products
      FROM exports e
      LEFT JOIN product_categories p ON p.sap_code=e.sap_code AND p.category=e.category
      GROUP BY e.sap_code, e.category
      ORDER BY e.category COLLATE NOCASE
    """).fetchall()
    result = []
    for r in rows:
        pages = sorted({int(x) for x in (r["pages_raw"] or "").split(",") if str(x).isdigit()})
        maximum = r["max_page_linked"]
        missing = []
        if maximum and int(maximum) <= 500:
            present = set(pages)
            missing = [n for n in range(1, int(maximum) + 1) if n not in present]
        result.append({
            "sap_code": r["sap_code"], "category": r["category"], "files": int(r["files_count"]),
            "page_count": int(r["page_count"] or 0), "pages": pages,
            "max_page_linked": maximum, "missing_pages_estimate": missing,
            "last_capture": r["last_capture"], "last_import": r["last_import"],
            "rows_count": int(r["rows_count"] or 0), "unique_products": int(r["unique_products"] or 0),
        })
    # Merge category checklist manifests (if exported from catalogue landing page).
    known: dict[str, dict] = {}
    for file in sorted(CATEGORY_DIR.glob("*.json")) if CATEGORY_DIR.exists() else []:
        try:
            payload = json.loads(file.read_text(encoding="utf-8-sig"))
            for c in payload.get("categories", []):
                name = str(c.get("name") or "").strip()
                if not name: continue
                key = name.casefold()
                known.setdefault(key, {"category": name, "url": c.get("url"), "category_id": c.get("category_id")})
        except Exception:
            continue
    have = {x["category"].casefold() for x in result}
    for key, c in known.items():
        if key not in have:
            result.append({"sap_code": EXPECTED_SAP, "category": c["category"], "files": 0,
                           "page_count": 0, "pages": [], "max_page_linked": None,
                           "missing_pages_estimate": [], "last_capture": None, "last_import": None,
                           "rows_count": 0, "unique_products": 0, "url": c.get("url"), "status": "Не начато"})
        else:
            for item in result:
                if item["category"].casefold() == key:
                    item["url"] = c.get("url")
    return result


def products_data(params: dict) -> dict:
    conn = connect_db()
    try:
        q = (params.get("q") or [""])[0].strip()[:120]
        category = (params.get("category") or [""])[0].strip()[:160]
        promo = (params.get("promo") or ["all"])[0]
        sort = (params.get("sort") or ["name"])[0]
        favorites_only = (params.get("favorites") or ["no"])[0] in {"1", "yes", "true"}
        page = max(1, min(int((params.get("page") or ["1"])[0]), 100000))
        limit = max(10, min(int((params.get("limit") or ["30"])[0]), 100))
        where, args = [], []
        if q:
            like = f"%{q}%"
            where.append("(p.name LIKE ? OR p.plu LIKE ? OR p.product_url LIKE ?)")
            args += [like, like, like]
        if category:
            where.append("EXISTS (SELECT 1 FROM product_categories pc WHERE pc.sap_code=p.sap_code AND pc.plu=p.plu AND pc.category=?)")
            args.append(category)
        if promo == "yes": where.append("p.discount_pct IS NOT NULL")
        elif promo == "no": where.append("p.discount_pct IS NULL")
        if favorites_only:
            where.append("EXISTS (SELECT 1 FROM favorites f WHERE f.sap_code=p.sap_code AND f.plu=p.plu)")
        sort_map = {
            "name": "p.name COLLATE NOCASE ASC", "price_asc": "p.current_price IS NULL, p.current_price ASC",
            "price_desc": "p.current_price DESC", "discount": "p.discount_pct DESC",
            "recent": "p.last_seen_at DESC", "first": "p.first_seen_at DESC",
            "increase": "p.current_price DESC",
        }
        order = sort_map.get(sort, sort_map["name"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute("SELECT COUNT(*) FROM products p" + clause, args).fetchone()[0]
        rows = conn.execute(f"""
          SELECT p.sap_code,p.plu,p.name,p.current_price,p.regular_price,p.promo_price,p.discount_pct,p.promo_label,
                 p.categories,p.image_url,p.image_file,p.product_url,p.first_seen_at,p.last_seen_at,
                 p.first_imported_at,p.last_imported_at,
                 (SELECT COUNT(*) FROM price_history h WHERE h.sap_code=p.sap_code AND h.plu=p.plu) AS observations,
                 EXISTS(SELECT 1 FROM favorites f WHERE f.sap_code=p.sap_code AND f.plu=p.plu) AS is_favorite,
                 EXISTS(SELECT 1 FROM product_details d WHERE d.sap_code=p.sap_code AND d.plu=p.plu) AS has_details
          FROM products p {clause} ORDER BY {order} LIMIT ? OFFSET ?
        """, args + [limit, (page - 1) * limit]).fetchall()
        items = []
        for r in rows:
            x = dict(r)
            x["local_image"] = f"/photos/{Path(str(x['image_file'])).name}" if x.get("image_file") else None
            x["is_favorite"] = bool(x.get("is_favorite"))
            x["has_details"] = bool(x.get("has_details"))
            items.append(x)
        cats = [r[0] for r in conn.execute("SELECT DISTINCT category FROM product_categories ORDER BY category COLLATE NOCASE")]
        return {"items": items, "total": total, "page": page, "limit": limit,
                "pages": (total + limit - 1)//limit, "categories": cats, "favorites_only": favorites_only}
    finally:
        conn.close()


def product_detail(params: dict) -> dict:
    sap = (params.get("sap") or [EXPECTED_SAP])[0]
    plu = (params.get("plu") or [""])[0]
    if not plu: raise ValueError("Не указан PLU")
    conn = connect_db()
    try:
        r = conn.execute("SELECT * FROM products WHERE sap_code=? AND plu=?", (sap, plu)).fetchone()
        if not r: raise KeyError("Товар не найден")
        product = dict(r)
        product["local_image"] = f"/photos/{Path(str(product['image_file'])).name}" if product.get("image_file") else None
        product["is_favorite"] = bool(conn.execute("SELECT 1 FROM favorites WHERE sap_code=? AND plu=?", (sap, plu)).fetchone())
        history = [dict(x) for x in conn.execute("""
            SELECT id,captured_at,imported_at,current_price,regular_price,promo_price,discount_pct,promo_label,
                   category,product_name,image_url,file_name,source_url
            FROM price_history WHERE sap_code=? AND plu=? ORDER BY captured_at,imported_at,id
        """, (sap, plu)).fetchall()]
        d = conn.execute("SELECT * FROM product_details WHERE sap_code=? AND plu=?", (sap, plu)).fetchone()
        extended = dict(d) if d else None
        if extended:
            for key in ("nutrition_json", "attributes_json"):
                try: extended[key.removesuffix("_json")] = json.loads(extended.get(key) or "{}")
                except Exception: extended[key.removesuffix("_json")] = {}
        detail_history = []
        if d:
            for row in conn.execute("""
                SELECT captured_at, imported_at, file_name, details_json
                FROM product_detail_history WHERE sap_code=? AND plu=? ORDER BY captured_at DESC, imported_at DESC LIMIT 20
            """, (sap, plu)).fetchall():
                item = dict(row)
                try: item["details"] = json.loads(item.pop("details_json"))
                except Exception: item["details"] = {}
                detail_history.append(item)
        return {"product": product, "history": history, "details": extended, "detail_history": detail_history}
    finally:
        conn.close()


def pending_exports_data() -> list[dict]:
    """Pending catalogue and product-detail JSON files, deduplicated by SHA-256."""
    import hashlib
    pending = []
    if EXPORT_DIR.exists():
        for path in sorted(EXPORT_DIR.rglob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict) or not isinstance(payload.get("products"), list) or not payload["products"]:
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                conn = connect_db()
                try: found = conn.execute("SELECT 1 FROM exports WHERE source_hash=?", (digest,)).fetchone()
                finally: conn.close()
                if not found:
                    pending.append({"name": path.name, "category": payload.get("category"),
                                    "products": len(payload["products"]), "captured_at": payload.get("captured_at"), "kind":"products"})
            except Exception:
                continue
    if DETAIL_DIR.exists():
        for path in sorted(DETAIL_DIR.rglob("*.json")):
            try:
                raw = path.read_bytes(); payload = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(payload, dict) or payload.get("source") not in {"5ka.ru-visible-product-details-export", "5ka.ru-visible-product-detail-export"}:
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                conn = connect_db()
                try: found = conn.execute("SELECT 1 FROM product_detail_imports WHERE source_hash=?", (digest,)).fetchone()
                finally: conn.close()
                if not found:
                    prod = payload.get("product") or {}
                    pending.append({"name":path.name,"category":"Доп. сведения товара",
                                    "products":1 if prod.get("plu") else 0,"captured_at":payload.get("captured_at"),"kind":"details"})
            except Exception:
                continue
    return pending


def exports_data() -> list[dict]:
    conn = connect_db()
    try:
        return [dict(r) for r in conn.execute("""
            SELECT file_name,category,sap_code,page_number,product_rows,unique_products,captured_at,imported_at,source_url
            FROM exports ORDER BY imported_at DESC
        """).fetchall()]
    finally:
        conn.close()


def persist_page_metadata() -> None:
    """Copy pagination facts produced by the newer visible-DOM bookmarklet into exports."""
    if not DB_PATH.exists(): return
    conn = connect_db()
    try:
        for path in EXPORT_DIR.rglob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                pagination = payload.get("pagination") or {}
                max_page = pagination.get("max_page_linked")
                visible = pagination.get("visible_page_numbers") or []
                if max_page is None and not visible: continue
                import hashlib
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                conn.execute("UPDATE exports SET max_page_linked=?, visible_page_numbers=? WHERE source_hash=?",
                             (int(max_page) if max_page is not None else None, safe_json(visible), digest))
            except Exception:
                continue
        conn.commit()
    finally:
        conn.close()


def normalize_details_payload(payload: dict) -> list[dict]:
    products = payload.get("products")
    if isinstance(products, list):
        return [x for x in products if isinstance(x, dict)]
    product = payload.get("product")
    return [product] if isinstance(product, dict) else []


def import_pending_details() -> tuple[int, int, int, list[str]]:
    """Import manual product-page detail snapshots into SQLite, preserving every import."""
    import hashlib
    if not DETAIL_DIR.exists(): return 0, 0, 0, []
    imported_files = imported_products = skipped = 0
    errors: list[str] = []
    conn = connect_db()
    try:
        for path in sorted(DETAIL_DIR.glob("*.json")):
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if conn.execute("SELECT 1 FROM product_detail_imports WHERE source_hash=?", (digest,)).fetchone():
                    skipped += 1; continue
                payload = json.loads(raw.decode("utf-8-sig"))
                if payload.get("source") not in {"5ka.ru-visible-product-details-export", "5ka.ru-visible-product-detail-export"}:
                    continue
                store = payload.get("store") or {}
                sap = str(store.get("sap_code") or payload.get("sap_code") or EXPECTED_SAP).strip()
                if sap != EXPECTED_SAP:
                    errors.append(f"{path.name}: SAP {sap}, ожидается {EXPECTED_SAP}"); continue
                captured = str(payload.get("captured_at") or now_iso())
                imported = now_iso()
                products = normalize_details_payload(payload)
                if not products:
                    errors.append(f"{path.name}: нет блока product/products"); continue
                conn.execute("BEGIN")
                accepted = 0
                for prod in products:
                    plu = str(prod.get("plu") or "").strip()
                    if not plu: continue
                    nutrition = prod.get("nutrition") if isinstance(prod.get("nutrition"), dict) else {}
                    attrs = prod.get("attributes") if isinstance(prod.get("attributes"), dict) else {}
                    detail_snapshot = {
                        "name": prod.get("name"), "product_url": prod.get("product_url"),
                        "image_url": prod.get("image_url"), "description": prod.get("description"),
                        "composition": prod.get("composition"), "nutrition": nutrition,
                        "attributes": attrs, "details_text": prod.get("details_text") or prod.get("raw_text"),
                    }
                    details_json = safe_json(detail_snapshot)
                    old = conn.execute("SELECT first_imported_at FROM product_details WHERE sap_code=? AND plu=?", (sap,plu)).fetchone()
                    first_imported = old[0] if old else imported
                    conn.execute("""
                        INSERT INTO product_detail_history(sap_code,plu,captured_at,imported_at,source_hash,file_name,details_json)
                        VALUES(?,?,?,?,?,?,?)
                        ON CONFLICT(source_hash,sap_code,plu) DO NOTHING
                    """, (sap,plu,captured,imported,digest,path.name,details_json))
                    conn.execute("""
                        INSERT INTO product_details(sap_code,plu,product_name,product_url,image_url,description,composition,
                            nutrition_json,attributes_json,details_text,first_imported_at,last_imported_at,last_captured_at,last_source_hash)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(sap_code,plu) DO UPDATE SET
                            product_name=COALESCE(excluded.product_name,product_details.product_name),
                            product_url=COALESCE(excluded.product_url,product_details.product_url),
                            image_url=COALESCE(excluded.image_url,product_details.image_url),
                            description=COALESCE(excluded.description,product_details.description),
                            composition=COALESCE(excluded.composition,product_details.composition),
                            nutrition_json=CASE WHEN excluded.nutrition_json!='{}' THEN excluded.nutrition_json ELSE product_details.nutrition_json END,
                            attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE product_details.attributes_json END,
                            details_text=COALESCE(excluded.details_text,product_details.details_text),
                            last_imported_at=excluded.last_imported_at,last_captured_at=excluded.last_captured_at,last_source_hash=excluded.last_source_hash
                    """, (sap,plu,prod.get("name"),prod.get("product_url"),prod.get("image_url"),prod.get("description"),
                          prod.get("composition"),safe_json(nutrition),safe_json(attrs),prod.get("details_text") or prod.get("raw_text"),
                          first_imported,imported,captured,digest))
                    accepted += 1
                if accepted == 0:
                    conn.rollback(); errors.append(f"{path.name}: не найден PLU"); continue
                conn.execute("INSERT INTO product_detail_imports(source_hash,file_name,captured_at,imported_at,product_count) VALUES(?,?,?,?,?)",
                             (digest,path.name,captured,imported,accepted))
                conn.commit()
                imported_files += 1; imported_products += accepted
            except Exception as exc:
                try: conn.rollback()
                except Exception: pass
                errors.append(f"{path.name}: {exc}")
    finally:
        conn.close()
    return imported_files, imported_products, skipped, errors


def analytics_data() -> dict:
    conn = connect_db()
    try:
        daily = [dict(r) for r in conn.execute("""
            SELECT substr(captured_at,1,10) AS day, ROUND(AVG(current_price),2) AS avg_price,
                   COUNT(*) AS observations,
                   ROUND(AVG(CASE WHEN discount_pct IS NOT NULL THEN discount_pct END),1) AS avg_discount
            FROM price_history WHERE current_price IS NOT NULL
            GROUP BY substr(captured_at,1,10) ORDER BY day DESC LIMIT 30
        """).fetchall()][::-1]
        top_discounts = [dict(r) for r in conn.execute("""
            SELECT sap_code,plu,name,current_price,regular_price,discount_pct,categories,image_file,image_url
            FROM products WHERE discount_pct IS NOT NULL ORDER BY discount_pct DESC,current_price ASC LIMIT 12
        """).fetchall()]
        changes = get_price_changes(conn, 2000, "all")
        ups = sorted([x for x in changes if x.get("delta") is not None and x["delta"]>0], key=lambda x:(x.get("delta_pct") or 0), reverse=True)[:10]
        downs = sorted([x for x in changes if x.get("delta") is not None and x["delta"]<0], key=lambda x:(x.get("delta_pct") or 0))[:10]
        summary = conn.execute("""
            SELECT COUNT(*) AS products, ROUND(AVG(current_price),2) AS avg_price,
                   ROUND(AVG(discount_pct),1) AS avg_discount, SUM(discount_pct IS NOT NULL) AS discounted
            FROM products
        """).fetchone()
        return {"daily":daily,"top_discounts":top_discounts,"biggest_up":ups,"biggest_down":downs,
                "summary":dict(summary)}
    finally: conn.close()


def favorites_data() -> list[dict]:
    conn = connect_db()
    try:
        rows = conn.execute("""
            SELECT p.sap_code,p.plu,p.name,p.current_price,p.regular_price,p.discount_pct,p.categories,
                   p.image_url,p.image_file,p.last_seen_at,f.created_at
            FROM favorites f JOIN products p ON p.sap_code=f.sap_code AND p.plu=f.plu
            ORDER BY f.created_at DESC
        """).fetchall()
        items=[]
        for r in rows:
            x=dict(r); x["local_image"] = f"/photos/{Path(str(x['image_file'])).name}" if x.get("image_file") else None
            items.append(x)
        return items
    finally: conn.close()


def set_favorite(data: dict) -> dict:
    sap = str(data.get("sap_code") or EXPECTED_SAP).strip()
    plu = str(data.get("plu") or "").strip()
    enabled = bool(data.get("favorite"))
    if not plu: raise ValueError("Не указан PLU")
    if sap != EXPECTED_SAP: raise ValueError(f"SAP {sap}, ожидается {EXPECTED_SAP}")
    conn=connect_db()
    try:
        exists=conn.execute("SELECT 1 FROM products WHERE sap_code=? AND plu=?",(sap,plu)).fetchone()
        if not exists: raise KeyError("Товар не найден")
        if enabled:
            conn.execute("INSERT OR IGNORE INTO favorites(sap_code,plu,created_at) VALUES(?,?,?)",(sap,plu,now_iso()))
        else:
            conn.execute("DELETE FROM favorites WHERE sap_code=? AND plu=?",(sap,plu))
        conn.commit()
        return {"sap_code":sap,"plu":plu,"favorite":enabled}
    finally: conn.close()


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.() -]+", "_", name, flags=re.UNICODE).strip(" .")
    if not name.lower().endswith(".json"):
        name += ".json"
    return name[:180] or f"upload_{int(time.time())}.json"


def upload_payload(data: dict) -> dict:
    files = data.get("files") or []
    if not isinstance(files, list) or not files or len(files) > 20:
        raise ValueError("Выберите от 1 до 20 JSON-файлов")
    saved, skipped = [], []
    EXPORT_DIR.mkdir(parents=True, exist_ok=True); CATEGORY_DIR.mkdir(parents=True, exist_ok=True); DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib
    for item in files:
        name = safe_filename(str(item.get("name") or "upload.json"))
        b64 = str(item.get("content_base64") or "")
        if len(b64) > (MAX_UPLOAD_BYTES * 4 // 3 + 16): raise ValueError(f"Файл {name} слишком большой (лимит 20 МБ)")
        try: raw = base64.b64decode(b64, validate=True)
        except Exception: raise ValueError(f"Файл {name}: неверное base64-содержимое")
        if len(raw) > MAX_UPLOAD_BYTES: raise ValueError(f"Файл {name} превышает 20 МБ")
        try: payload = json.loads(raw.decode("utf-8-sig"))
        except Exception: raise ValueError(f"Файл {name}: не удалось прочитать JSON")
        source = payload.get("source") if isinstance(payload, dict) else None
        if source == "5ka.ru-visible-category-links" and isinstance(payload.get("categories"), list):
            dest_dir, kind = CATEGORY_DIR, "categories"
        elif source in {"5ka.ru-visible-product-details-export", "5ka.ru-visible-product-detail-export"} and (payload.get("product") or payload.get("products")):
            store = payload.get("store") or {}; sap = store.get("sap_code") or payload.get("sap_code") or EXPECTED_SAP
            if str(sap) != EXPECTED_SAP: raise ValueError(f"{name}: SAP {sap}, ожидается {EXPECTED_SAP}")
            dest_dir, kind = DETAIL_DIR, "details"
        elif isinstance(payload, dict) and isinstance(payload.get("products"), list) and payload.get("products"):
            store = payload.get("store") or {}; sap = store.get("sap_code") or payload.get("store_sap_code")
            if sap and str(sap) != EXPECTED_SAP: raise ValueError(f"{name}: SAP {sap}, ожидается {EXPECTED_SAP}. Файл не сохранён")
            dest_dir, kind = EXPORT_DIR, "products"
        else:
            raise ValueError(f"{name}: это не экспорт товаров, доп. сведений или категорий 5ka.ru")
        digest = hashlib.sha256(raw).hexdigest(); dest = dest_dir / name
        if dest.exists():
            if hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
                skipped.append(name); continue
            dest = dest_dir / f"{Path(name).stem}_{digest[:8]}.json"
        dest.write_bytes(raw)
        product_count = len(payload.get("products") or []) if isinstance(payload.get("products"),list) else (1 if payload.get("product") else 0)
        saved.append({"name": dest.name, "kind": kind, "products": product_count,
                      "category": payload.get("category"), "captured_at": payload.get("captured_at"),
                      "source_url": payload.get("source_url")})
    return {"saved": saved, "skipped": skipped}


def run_import(download_photos: bool = False) -> None:
    global _JOB
    with _JOB_LOCK:
        if _JOB["running"]: return
        _JOB["running"] = True
        _JOB["job_id"] += 1
        job_id = _JOB["job_id"]
        _JOB.update({"started_at": now_iso(), "finished_at": None, "return_code": None,
                     "output": "Запускаю импорт…", "error": None})
    try:
        detail_files, detail_products, detail_skipped, detail_errors = import_pending_details()
        detail_line = (f"Доп. сведения: новых файлов {detail_files}, товаров обновлено {detail_products}, "
                       f"повторных файлов пропущено {detail_skipped}.\n")
        if detail_errors:
            detail_line += "Ошибки доп. сведений: " + "; ".join(detail_errors[:8]) + "\n"
        with _JOB_LOCK:
            if _JOB["job_id"] == job_id:
                _JOB["output"] = detail_line + "Запускаю импорт товарных выгрузок…\n"
        cmd = [sys.executable, str(IMPORTER_PATH)]
        if not download_photos: cmd.append("--no-photos")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Make child Python output UTF-8 before decoding its pipe on Windows.
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1, env=env)
        lines: list[str] = [detail_line]
        started = time.monotonic()
        assert proc.stdout is not None
        while True:
            line = proc.stdout.readline()
            if line:
                lines.append(line)
                if sum(map(len, lines)) > 24000:
                    lines = lines[-300:]
                with _JOB_LOCK:
                    if _JOB["job_id"] == job_id:
                        _JOB["output"] = "".join(lines)[-24000:]
            elif proc.poll() is not None:
                break
            if time.monotonic() - started > 60 * 60:
                proc.kill()
                raise TimeoutError("Импорт превысил лимит 60 минут и был остановлен.")
        return_code = proc.wait()
        out = "".join(lines)
        persist_page_metadata()
        with _JOB_LOCK:
            if _JOB["job_id"] == job_id:
                _JOB.update({"running": False, "finished_at": now_iso(), "return_code": return_code,
                             "output": out[-24000:] or "Импорт завершён без текстового вывода.",
                             "error": None if return_code == 0 else f"Импорт завершился с кодом {return_code}"})
    except Exception as exc:
        with _JOB_LOCK:
            if _JOB["job_id"] == job_id:
                _JOB.update({"running": False, "finished_at": now_iso(), "return_code": -1,
                             "output": _JOB.get("output", ""), "error": str(exc)})


def open_local_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Handler(BaseHTTPRequestHandler):
    server_version = "PyaterochkaCatalogUI/11.1"

    def log_message(self, fmt, *args):
        # Keep console output small and avoid logging query strings with product search.
        print(f"[UI] {self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8", extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if extra:
            for k, v in extra.items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data):
        self._send(status, safe_json(data).encode("utf-8"))

    def _body_json(self):
        n = int(self.headers.get("Content-Length", "0"))
        if n <= 0 or n > 32 * 1024 * 1024: raise ValueError("Некорректный размер запроса")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)
        try:
            if path == "/": return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(unquote(path[len("/static/"):]))
            if path.startswith("/photos/"):
                name = Path(unquote(path[len("/photos/"):])).name
                return self._file(PHOTO_DIR / name)
            if path == "/files/excel": return self._file(BASE_DIR / "pyaterochka_catalog_v9.xlsx", attachment=True)
            if path == "/files/csv": return self._file(BASE_DIR / "pyaterochka_catalog_v9.csv", attachment=True)
            if path == "/api/health": return self._json(200, {"ok": True, "db_exists": DB_PATH.exists(), "db": DB_PATH.name})
            if path == "/api/dashboard": return self._json(200, dashboard_data())
            if path == "/api/analytics": return self._json(200, analytics_data())
            if path == "/api/products": return self._json(200, products_data(params))
            if path == "/api/product": return self._json(200, product_detail(params))
            if path == "/api/favorites": return self._json(200, favorites_data())
            if path == "/api/changes":
                conn = connect_db()
                try:
                    direction = (params.get("direction") or ["all"])[0]
                    limit = int((params.get("limit") or ["300"])[0])
                    return self._json(200, get_price_changes(conn, limit, direction))
                finally: conn.close()
            if path == "/api/categories":
                conn = connect_db()
                try: return self._json(200, category_progress(conn))
                finally: conn.close()
            if path == "/api/exports": return self._json(200, exports_data())
            if path == "/api/pending": return self._json(200, pending_exports_data())
            if path == "/api/job":
                with _JOB_LOCK: return self._json(200, dict(_JOB))
            if path == "/api/folders":
                return self._json(200, {"root": str(BASE_DIR), "exports": str(EXPORT_DIR), "photos": str(PHOTO_DIR), "details": str(DETAIL_DIR), "database": str(DB_PATH)})
            return self._json(404, {"error": "Маршрут не найден"})
        except FileNotFoundError as exc: return self._json(409, {"error": str(exc)})
        except (ValueError, KeyError) as exc: return self._json(400, {"error": str(exc)})
        except Exception as exc: return self._json(500, {"error": f"Ошибка интерфейса: {exc}"})

    def _static(self, relative: str):
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            return self._json(403, {"error": "Недопустимый путь"})
        if not candidate.is_file(): return self._json(404, {"error": "Файл интерфейса не найден"})
        return self._file(candidate)

    def _file(self, path: Path, attachment: bool = False):
        try:
            resolved = path.resolve()
            allowed = [BASE_DIR.resolve(), PHOTO_DIR.resolve()]
            if not any(root == resolved or root in resolved.parents for root in allowed):
                return self._json(403, {"error": "Недопустимый путь"})
            raw = resolved.read_bytes()
            ctype = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            extra = {}
            if attachment:
                safe = re.sub(r'[^\w.-]+', '_', resolved.name)
                extra["Content-Disposition"] = f'attachment; filename="{safe}"'
            return self._send(200, raw, ctype, extra)
        except FileNotFoundError:
            return self._json(404, {"error": "Файл пока не создан"})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self._body_json()
            if parsed.path == "/api/upload":
                return self._json(200, upload_payload(data))
            if parsed.path == "/api/favorite":
                return self._json(200, set_favorite(data))
            if parsed.path == "/api/import":
                if not IMPORTER_PATH.exists(): raise FileNotFoundError(f"Не найден {IMPORTER_PATH.name}")
                download = bool(data.get("download_photos"))
                with _JOB_LOCK:
                    if _JOB["running"]: return self._json(409, {"error": "Импорт уже выполняется"})
                thread = threading.Thread(target=run_import, args=(download,), daemon=True)
                thread.start()
                return self._json(202, {"started": True, "download_photos": download})
            if parsed.path == "/api/open-folder":
                kind = str(data.get("kind") or "exports")
                folder = {"root": BASE_DIR, "exports": EXPORT_DIR, "photos": PHOTO_DIR, "categories": CATEGORY_DIR, "details": DETAIL_DIR}.get(kind)
                if folder is None: raise ValueError("Неизвестная папка")
                open_local_folder(folder)
                return self._json(200, {"opened": str(folder)})
            return self._json(404, {"error": "Маршрут не найден"})
        except FileNotFoundError as exc: return self._json(409, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc: return self._json(400, {"error": str(exc)})
        except Exception as exc: return self._json(500, {"error": str(exc)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальный веб-интерфейс товарной базы Пятёрочки")
    parser.add_argument("--host", default="127.0.0.1", help="По умолчанию доступен только на этом компьютере")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Безопасность: интерфейс разрешено запускать только на loopback (127.0.0.1).")
        return 2
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    CATEGORY_DIR.mkdir(parents=True, exist_ok=True)
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    try: ensure_ui_columns()
    except Exception as exc: print(f"Предупреждение: не удалось добавить метаданные страниц: {exc}")
    url = f"http://127.0.0.1:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("=" * 64)
    print("PYATEROCHKA CATALOG — локальный интерфейс V11.1")
    print(f"Адрес: {url}")
    print(f"Папка проекта: {BASE_DIR}")
    print("Сервер доступен только с этого компьютера. Для остановки нажмите Ctrl+C.")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt: print("\nОстановка интерфейса…")
    finally: server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
