# -*- coding: utf-8 -*-
"""Накопительный импортёр каталога 5ka.ru: SQLite + Excel + история цен.

Работает только с файлами, которые пользователь экспортировал из видимой страницы
5ka.ru. Не открывает сайт, не использует cookies и не обращается к API каталога.

Запуск из папки проекта:
    python import_5ka_catalog_v8.py
    python import_5ka_catalog_v8.py --no-photos
    python import_5ka_catalog_v8.py --expected-sap 35XY

Новые JSON-файлы помещайте в 5ka_exports/. Повторный запуск безопасен:
файл с уже обработанным SHA-256 не добавляется в историю второй раз.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from PIL import Image
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.table import Table, TableStyleInfo

BASE_DIR = Path(__file__).resolve().parent
EXPORT_DIR = BASE_DIR / "5ka_exports"
LEGACY_DIR = BASE_DIR / "catalog_json"
CHECKPOINT_PATH = BASE_DIR / "pyaterochka_checkpoint.jsonl"
DB_PATH = BASE_DIR / "pyaterochka_catalog_v8.sqlite3"
OUTPUT_XLSX = BASE_DIR / "pyaterochka_catalog_v8.xlsx"
OUTPUT_CSV = BASE_DIR / "pyaterochka_catalog_v8.csv"
REPORT_JSON = BASE_DIR / "pyaterochka_catalog_v8_report.json"
PHOTO_DIR = BASE_DIR / "photos5ka"

PRODUCT_COLUMNS = [
    "SAP магазина", "PLU", "название", "цена сейчас, ₽", "обычная цена, ₽",
    "цена по акции, ₽", "скидка, %", "маркер акции", "категории",
    "фото URL", "фото файл", "карточка товара", "первое появление",
    "последнее наблюдение", "наблюдений цены",
]
HISTORY_COLUMNS = [
    "дата получения", "SAP магазина", "PLU", "название", "категория",
    "цена сейчас, ₽", "обычная цена, ₽", "цена по акции, ₽", "скидка, %",
    "маркер акции", "страница каталога", "файл выгрузки",
]
CHANGE_COLUMNS = [
    "дата изменения", "SAP магазина", "PLU", "название", "категория",
    "предыдущая цена, ₽", "новая цена, ₽", "изменение, ₽", "изменение, %",
    "предыдущая скидка, %", "новая скидка, %", "что изменилось",
]
PROGRESS_COLUMNS = [
    "SAP магазина", "категория", "страниц выгружено", "номера страниц",
    "файлов выгрузки", "уникальных PLU", "последняя выгрузка", "цены", "фото URL",
]
EXPORT_COLUMNS = [
    "дата выгрузки", "SAP магазина", "категория", "страница",
    "товаров в файле", "имя файла", "исходная страница", "SHA-256",
]

PRICE_RE = re.compile(
    r"(?<!\d)(\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d{2}|[\u00a0\u202f ]\d{2})?)\s*₽",
    re.IGNORECASE,
)
OLD_PRICE_RE = re.compile(
    r"старая\s+цена\s*(\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d{2}|[\u00a0\u202f ]\d{2})?)\s*₽",
    re.IGNORECASE,
)
PROMO_RE = re.compile(r"(?:-\s*\d{1,2}\s*%|%\s*от\s*\d+\s*шт|\b1\s*\+\s*1\b|\bакци\w*\b)", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_timestamp(value: Any, fallback: str | None = None) -> str:
    if value:
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            return text
    return fallback or utc_now()


def to_price(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).replace("₽", "").replace("\u00a0", " ").replace("\u202f", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    kopecks = re.fullmatch(r"(\d+(?: \d{3})*) (\d{2})", text)
    if kopecks:
        left, right = kopecks.groups()
        text = f"{left.replace(' ', '')}.{right}"
    else:
        text = text.replace(" ", "")
    try:
        return round(float(text.replace(",", ".")), 2)
    except ValueError:
        return None


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def first_image_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        for item in value:
            found = first_image_url(item)
            if found:
                return found
    elif isinstance(value, dict):
        for key in ("normal", "small", "link", "src", "url", "product_card"):
            if key in value:
                found = first_image_url(value[key])
                if found:
                    return found
    return None


def parse_visible_card(raw_text: str) -> tuple[float | None, float | None, float | None, str | None, float | None]:
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    prices = [to_price(m.group(1)) for m in PRICE_RE.finditer(text)]
    prices = [p for p in prices if p is not None]
    old_match = OLD_PRICE_RE.search(text)
    old_price = to_price(old_match.group(1)) if old_match else None
    current = prices[-1] if prices else None
    regular = old_price
    discount = current if old_price is not None and current is not None and current < old_price else None
    promo_match = PROMO_RE.search(text)
    promo_label = promo_match.group(0) if promo_match else None
    percent_match = re.search(r"-\s*(\d{1,2})\s*%", text)
    if regular and current is not None and regular > current:
        discount_pct = round((regular - current) / regular * 100, 1)
    elif percent_match:
        discount_pct = float(percent_match.group(1))
        if current is not None:
            discount = current
    else:
        discount_pct = None
    return current, regular, discount, promo_label, discount_pct


def clean_title(title: Any, raw_text: str) -> str | None:
    candidate = str(title or "").strip()
    if candidate:
        return candidate
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    for pattern in (
        r"-\s*\d{1,2}\s*%", r"%\s*от\s*\d+\s*шт", r"\b1\s*\+\s*1\b",
        r"Старая\s+цена.*", r"Цена\s+.*", r"\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d{2}|[\u00a0\u202f ]\d{2})?\s*₽",
        r"В корзину|Нет оценок|Сейчас сезон|Уже сварено",
    ):
        text = re.sub(pattern, " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -|·")[:250] or None


def row_from_visible_export(payload: dict[str, Any], product: dict[str, Any]) -> dict[str, Any] | None:
    store = payload.get("store") or {}
    raw_text = str(product.get("raw_text") or "")
    current, regular, promo_price, promo_label, discount_pct = parse_visible_card(raw_text)
    return {
        "SAP магазина": first_present(store.get("sap_code"), payload.get("store_sap_code")),
        "PLU": str(first_present(product.get("plu"), product.get("id"), "")).strip() or None,
        "название": clean_title(first_present(product.get("name"), product.get("image_alt")), raw_text),
        "цена сейчас, ₽": current,
        "обычная цена, ₽": regular,
        "цена по акции, ₽": promo_price,
        "скидка, %": discount_pct,
        "маркер акции": promo_label,
        "категории": first_present(payload.get("category"), "не указана"),
        "фото URL": first_present(product.get("image_url"), product.get("image")),
        "карточка товара": product.get("product_url"),
        "страница каталога": payload.get("source_url"),
        "время получения": normalize_timestamp(payload.get("captured_at")),
        "сырой текст карточки": raw_text,
    }


def row_from_api_product(product: dict[str, Any], category: str, sap: str | None) -> dict[str, Any]:
    prices = product.get("prices") or {}
    regular = to_price(first_present(prices.get("regular"), prices.get("priceRegular"), product.get("oldPrice")))
    promo = to_price(first_present(prices.get("discount"), prices.get("discountPrice"), product.get("salePrice")))
    current = to_price(first_present(prices.get("current"), prices.get("price"), product.get("price"), promo, regular))
    pct = round((regular - current) / regular * 100, 1) if regular and current is not None and current < regular else None
    media = product.get("media") or {}
    image = first_image_url(first_present(product.get("image_links"), product.get("image"), product.get("image_link"), media.get("image_links")))
    return {
        "SAP магазина": sap,
        "PLU": str(first_present(product.get("plu"), product.get("id"), "")).strip() or None,
        "название": first_present(product.get("name"), product.get("title")),
        "цена сейчас, ₽": current,
        "обычная цена, ₽": regular if regular is not None else current,
        "цена по акции, ₽": promo if promo is not None else (current if pct is not None else None),
        "скидка, %": pct,
        "маркер акции": None,
        "категории": category,
        "фото URL": image,
        "карточка товара": first_present(product.get("url"), product.get("product_url")),
        "страница каталога": None,
        "время получения": utc_now(),
        "сырой текст карточки": None,
    }


def walk_products(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        if (obj.get("plu") is not None or obj.get("id") is not None) and (obj.get("name") or obj.get("title")):
            yield obj
            return
        for value in obj.values():
            yield from walk_products(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_products(value)


def load_json_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, dict) and payload.get("source") == "5ka.ru-visible-category-links":
        return [], {"is_checklist": True}
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        products = payload["products"]
        is_visible = payload.get("source") == "5ka.ru-visible-page-export" or any(
            isinstance(p, dict) and "raw_text" in p for p in products
        )
        if is_visible:
            for product in products:
                if isinstance(product, dict):
                    row = row_from_visible_export(payload, product)
                    if row and row.get("PLU"):
                        rows.append(row)
        else:
            category = str(payload.get("category") or path.stem)
            sap = (payload.get("store") or {}).get("sap_code")
            for product in walk_products(products):
                row = row_from_api_product(product, category, sap)
                row["время получения"] = normalize_timestamp(payload.get("captured_at"), utc_now())
                if row.get("PLU"):
                    rows.append(row)
    else:
        for product in walk_products(payload):
            row = row_from_api_product(product, path.stem, None)
            if row.get("PLU"):
                rows.append(row)
    meta = {
        "category": payload.get("category") if isinstance(payload, dict) else path.stem,
        "source_url": payload.get("source_url") if isinstance(payload, dict) else None,
        "sap_code": ((payload.get("store") or {}).get("sap_code") if isinstance(payload, dict) else None),
        "captured_at": normalize_timestamp(payload.get("captured_at") if isinstance(payload, dict) else None, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")),
        "collection_mode": payload.get("collection_mode") if isinstance(payload, dict) else None,
    }
    return rows, meta


def load_checkpoint_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            print(f"  checkpoint: повреждена строка {line_no}, пропуск")
            continue
        category = entry.get("category") or "не указана"
        for item in entry.get("rows") or []:
            plu = first_present(item.get("id (plu)"), item.get("plu"), item.get("id"))
            if not plu:
                continue
            current = to_price(item.get("цена, ₽"))
            regular = to_price(item.get("обычная цена, ₽"))
            pct = round((regular - current) / regular * 100, 1) if regular and current is not None and current < regular else None
            rows.append({
                "SAP магазина": "35XY", "PLU": str(plu), "название": item.get("название") or item.get("name"),
                "цена сейчас, ₽": current, "обычная цена, ₽": regular,
                "цена по акции, ₽": current if pct is not None else None, "скидка, %": pct,
                "маркер акции": None, "категории": item.get("категория") or category,
                "фото URL": item.get("фото"), "карточка товара": None, "страница каталога": None,
                "время получения": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "сырой текст карточки": None,
            })
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def page_number(source_url: str | None) -> str | None:
    if not source_url:
        return None
    try:
        values = parse_qs(urlparse(source_url).query).get("page")
        return values[0] if values else "1"
    except Exception:
        return None


class CatalogDB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS exports (
            source_hash TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            category TEXT,
            source_url TEXT,
            sap_code TEXT,
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            product_rows INTEGER NOT NULL DEFAULT 0,
            unique_products INTEGER NOT NULL DEFAULT 0,
            page_number TEXT
        );
        CREATE TABLE IF NOT EXISTS products (
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            name TEXT,
            current_price REAL,
            regular_price REAL,
            promo_price REAL,
            discount_pct REAL,
            promo_label TEXT,
            categories TEXT,
            image_url TEXT,
            image_file TEXT,
            product_url TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_source_hash TEXT,
            PRIMARY KEY (sap_code, plu)
        );
        CREATE TABLE IF NOT EXISTS product_categories (
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            category TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (sap_code, plu, category),
            FOREIGN KEY (sap_code, plu) REFERENCES products(sap_code, plu) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_hash TEXT NOT NULL,
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            category TEXT,
            captured_at TEXT NOT NULL,
            current_price REAL,
            regular_price REAL,
            promo_price REAL,
            discount_pct REAL,
            promo_label TEXT,
            raw_text TEXT,
            source_url TEXT,
            file_name TEXT NOT NULL,
            UNIQUE (source_hash, sap_code, plu),
            FOREIGN KEY (source_hash) REFERENCES exports(source_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_history_product_time ON price_history(sap_code, plu, captured_at);
        CREATE INDEX IF NOT EXISTS idx_history_time ON price_history(captured_at);
        """)
        self.conn.commit()

    def has_export(self, source_hash: str) -> bool:
        return self.conn.execute("SELECT 1 FROM exports WHERE source_hash=?", (source_hash,)).fetchone() is not None

    def import_rows(self, source_hash: str, file_name: str, rows: list[dict[str, Any]], meta: dict[str, Any]) -> tuple[int, int]:
        if self.has_export(source_hash):
            return 0, 0
        captured = normalize_timestamp(meta.get("captured_at"), utc_now())
        sap_from_meta = meta.get("sap_code")
        unique_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            plu = str(row.get("PLU") or "").strip()
            if not plu:
                continue
            sap = str(first_present(row.get("SAP магазина"), sap_from_meta, "НЕ УКАЗАН")).strip()
            normalized = dict(row)
            normalized["PLU"] = plu
            normalized["SAP магазина"] = sap
            normalized["время получения"] = normalize_timestamp(row.get("время получения"), captured)
            key = (sap, plu)
            unique_rows[key] = normalized

        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """INSERT INTO exports(source_hash,file_name,category,source_url,sap_code,captured_at,imported_at,product_rows,unique_products,page_number)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (source_hash, file_name, meta.get("category"), meta.get("source_url"), sap_from_meta,
                 captured, utc_now(), len(rows), len(unique_rows), page_number(meta.get("source_url"))),
            )
            for (sap, plu), row in unique_rows.items():
                seen_at = normalize_timestamp(row.get("время получения"), captured)
                category = str(row.get("категории") or meta.get("category") or "не указана").strip()
                current = row.get("цена сейчас, ₽")
                regular = row.get("обычная цена, ₽")
                promo = row.get("цена по акции, ₽")
                discount_pct = row.get("скидка, %")
                existing = self.conn.execute(
                    "SELECT first_seen_at,last_seen_at FROM products WHERE sap_code=? AND plu=?", (sap, plu)
                ).fetchone()
                if existing is None:
                    self.conn.execute(
                        """INSERT INTO products(sap_code,plu,name,current_price,regular_price,promo_price,discount_pct,promo_label,categories,image_url,image_file,product_url,first_seen_at,last_seen_at,last_source_hash)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sap, plu, row.get("название"), current, regular, promo, discount_pct, row.get("маркер акции"),
                         category, row.get("фото URL"), None, row.get("карточка товара"), seen_at, seen_at, source_hash),
                    )
                else:
                    first_seen = min(str(existing["first_seen_at"]), seen_at)
                    is_newer = seen_at >= str(existing["last_seen_at"])
                    if is_newer:
                        self.conn.execute(
                            """UPDATE products SET name=COALESCE(?,name), current_price=?, regular_price=?, promo_price=?, discount_pct=?, promo_label=?,
                               image_url=COALESCE(?,image_url), product_url=COALESCE(?,product_url), first_seen_at=?, last_seen_at=?, last_source_hash=?
                               WHERE sap_code=? AND plu=?""",
                            (row.get("название"), current, regular, promo, discount_pct, row.get("маркер акции"),
                             row.get("фото URL"), row.get("карточка товара"), first_seen, seen_at, source_hash, sap, plu),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE products SET first_seen_at=? WHERE sap_code=? AND plu=?",
                            (first_seen, sap, plu),
                        )
                self.conn.execute(
                    """INSERT INTO product_categories(sap_code,plu,category,first_seen_at,last_seen_at) VALUES(?,?,?,?,?)
                       ON CONFLICT(sap_code,plu,category) DO UPDATE SET last_seen_at=MAX(last_seen_at,excluded.last_seen_at), first_seen_at=MIN(first_seen_at,excluded.first_seen_at)""",
                    (sap, plu, category, seen_at, seen_at),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO price_history(source_hash,sap_code,plu,category,captured_at,current_price,regular_price,promo_price,discount_pct,promo_label,raw_text,source_url,file_name)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (source_hash, sap, plu, category, seen_at, current, regular, promo, discount_pct,
                     row.get("маркер акции"), row.get("сырой текст карточки"), meta.get("source_url"), file_name),
                )
            # Refresh category string after adding all product/category relationships.
            for sap, plu in unique_rows:
                cats = [r[0] for r in self.conn.execute(
                    "SELECT category FROM product_categories WHERE sap_code=? AND plu=? ORDER BY category", (sap, plu)
                ).fetchall()]
                self.conn.execute("UPDATE products SET categories=? WHERE sap_code=? AND plu=?", (" | ".join(cats), sap, plu))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(rows), len(unique_rows)

    def update_image(self, sap: str, plu: str, image_file: str) -> None:
        self.conn.execute("UPDATE products SET image_file=? WHERE sap_code=? AND plu=?", (image_file, sap, plu))
        self.conn.commit()

    def products(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM price_history h WHERE h.sap_code=p.sap_code AND h.plu=p.plu) AS observation_count
            FROM products p ORDER BY p.sap_code,p.categories,p.name
        """).fetchall()
        result = []
        for r in rows:
            result.append({
                "SAP магазина": r["sap_code"], "PLU": r["plu"], "название": r["name"],
                "цена сейчас, ₽": r["current_price"], "обычная цена, ₽": r["regular_price"],
                "цена по акции, ₽": r["promo_price"], "скидка, %": r["discount_pct"],
                "маркер акции": r["promo_label"], "категории": r["categories"],
                "фото URL": r["image_url"], "фото файл": r["image_file"],
                "карточка товара": r["product_url"], "первое появление": r["first_seen_at"],
                "последнее наблюдение": r["last_seen_at"], "наблюдений цены": r["observation_count"],
            })
        return result

    def history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT h.captured_at,p.name,h.sap_code,h.plu,h.category,h.current_price,h.regular_price,h.promo_price,h.discount_pct,h.promo_label,h.source_url,h.file_name
            FROM price_history h LEFT JOIN products p ON p.sap_code=h.sap_code AND p.plu=h.plu
            ORDER BY h.captured_at,h.sap_code,h.plu
        """).fetchall()
        keys = ["дата получения","название","SAP магазина","PLU","категория","цена сейчас, ₽","обычная цена, ₽","цена по акции, ₽","скидка, %","маркер акции","страница каталога","файл выгрузки"]
        return [dict(zip(keys, tuple(r))) for r in rows]

    def changes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT h.captured_at,h.sap_code,h.plu,p.name,h.category,h.current_price,h.regular_price,h.promo_price,h.discount_pct
            FROM price_history h LEFT JOIN products p ON p.sap_code=h.sap_code AND p.plu=h.plu
            ORDER BY h.sap_code,h.plu,h.captured_at,h.id
        """).fetchall()
        previous: dict[tuple[str,str], sqlite3.Row] = {}
        output: list[dict[str, Any]] = []
        for r in rows:
            key = (r["sap_code"], r["plu"])
            old = previous.get(key)
            previous[key] = r
            if old is None:
                continue
            if old["current_price"] is None or r["current_price"] is None:
                continue
            old_price = float(old["current_price"])
            new_price = float(r["current_price"])
            old_discount = old["discount_pct"]
            new_discount = r["discount_pct"]
            old_regular = old["regular_price"]
            new_regular = r["regular_price"]
            labels = []
            if old_price != new_price:
                labels.append("текущая цена")
            if old_regular != new_regular:
                labels.append("обычная цена")
            if old_discount != new_discount:
                labels.append("скидка")
            if not labels:
                continue
            delta = round(new_price - old_price, 2)
            delta_pct = round(delta / old_price * 100, 2) if old_price else None
            output.append({
                "дата изменения": r["captured_at"], "SAP магазина": r["sap_code"], "PLU": r["plu"],
                "название": r["name"], "категория": r["category"],
                "предыдущая цена, ₽": old_price, "новая цена, ₽": new_price,
                "изменение, ₽": delta, "изменение, %": delta_pct,
                "предыдущая скидка, %": old_discount, "новая скидка, %": new_discount,
                "что изменилось": ", ".join(labels),
            })
        return output

    def exports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT captured_at,sap_code,category,page_number,product_rows,file_name,source_url,source_hash FROM exports ORDER BY captured_at").fetchall()
        keys = ["дата выгрузки","SAP магазина","категория","страница","товаров в файле","имя файла","исходная страница","SHA-256"]
        return [dict(zip(keys, tuple(r))) for r in rows]

    def progress(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        exports = self.exports()
        by_cat: dict[tuple[str,str], list[dict[str, Any]]] = defaultdict(list)
        for e in exports:
            by_cat[(str(e.get("SAP магазина") or "НЕ УКАЗАН"), str(e.get("категория") or "не указана"))].append(e)
        product_map: dict[tuple[str,str], list[dict[str, Any]]] = defaultdict(list)
        for p in products:
            sap = str(p.get("SAP магазина") or "НЕ УКАЗАН")
            for cat in str(p.get("категории") or "не указана").split(" | "):
                product_map[(sap, cat)].append(p)
        output=[]
        for key, files in sorted(by_cat.items()):
            items = product_map.get(key, [])
            pages = sorted({str(e["страница"]) for e in files if e.get("страница") is not None}, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
            output.append({
                "SAP магазина": key[0], "категория": key[1], "страниц выгружено": len(pages) if pages else None,
                "номера страниц": ", ".join(pages) if pages else "не определено",
                "файлов выгрузки": len(files), "уникальных PLU": len({p["PLU"] for p in items}),
                "последняя выгрузка": max(str(e.get("дата выгрузки") or "") for e in files),
                "цены": sum(p.get("цена сейчас, ₽") is not None for p in items),
                "фото URL": sum(bool(p.get("фото URL")) for p in items),
            })
        return output

    def close(self) -> None:
        self.conn.close()


def save_image(url: str | None, sap: str, plu: str, delay: float) -> str | None:
    if not url or not str(url).startswith("https://"):
        return None
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    safe_plu = re.sub(r"[^A-Za-z0-9_-]", "_", str(plu or "unknown"))
    old_path = PHOTO_DIR / f"{safe_plu}.jpg"
    # Reuse the v7 filename when it is already present.
    if old_path.exists() and old_path.stat().st_size > 0:
        return str(old_path.relative_to(BASE_DIR))
    safe_sap = re.sub(r"[^A-Za-z0-9_-]", "_", str(sap or "store"))
    dest = PHOTO_DIR / f"{safe_sap}_{safe_plu}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest.relative_to(BASE_DIR))
    try:
        response = requests.get(url, timeout=25, headers={"Referer": "https://5ka.ru/"})
        response.raise_for_status()
        with Image.open(BytesIO(response.content)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                out = background
            else:
                out = img.convert("RGB")
            out.save(dest, "JPEG", quality=90, optimize=True)
        if delay > 0:
            time.sleep(delay)
        return str(dest.relative_to(BASE_DIR))
    except Exception as exc:
        print(f"  Фото PLU {plu}: не скачано ({exc})")
        return None


def make_summary(products: list[dict[str, Any]], history: list[dict[str, Any]], changes: list[dict[str, Any]], exports: list[dict[str, Any]], expected_sap: str) -> pd.DataFrame:
    sap_codes = sorted({str(p.get("SAP магазина")) for p in products if p.get("SAP магазина") and p.get("SAP магазина") != "НЕ УКАЗАН"})
    metrics = [
        ("Магазин SAP (ожидаемый)", expected_sap),
        ("Фактически найденные SAP", ", ".join(sap_codes) if sap_codes else "не указаны"),
        ("Товаров в текущей базе", len(products)),
        ("Товаров с текущей ценой", sum(p.get("цена сейчас, ₽") is not None for p in products)),
        ("Товаров со скидкой", sum(p.get("скидка, %") is not None for p in products)),
        ("Товаров с URL фото", sum(bool(p.get("фото URL")) for p in products)),
        ("Локальных фото", sum(bool(p.get("фото файл")) for p in products)),
        ("Наблюдений цены в истории", len(history)),
        ("Зафиксированных изменений цены/скидки", len(changes)),
        ("Импортированных файлов выгрузки", len(exports)),
        ("Дата обновления базы", utc_now()),
        ("Важное замечание", "Это цена онлайн-каталога для выбранного адреса доставки; она может отличаться от цены на полке."),
    ]
    return pd.DataFrame(metrics, columns=["Показатель", "Значение"])


def style_workbook(path: Path) -> None:
    wb = load_workbook(path)
    header_fill = PatternFill("solid", fgColor="166534")
    header_font = Font(bold=True, color="FFFFFF")
    table_names = {
        "Товары": "CurrentProducts", "История цен": "PriceHistory",
        "Изменения цен": "PriceChanges", "Прогресс категорий": "CategoryProgress", "Экспорты": "CatalogExports",
    }
    widths = {
        "Товары": {"A": 14,"B": 12,"C": 44,"D": 16,"E": 16,"F": 16,"G": 12,"H": 20,"I": 40,"J": 48,"K": 32,"L": 48,"M": 24,"N": 24,"O": 18},
        "История цен": {"A": 24,"B": 14,"C": 12,"D": 42,"E": 36,"F": 16,"G": 16,"H": 16,"I": 12,"J": 20,"K": 48,"L": 48},
        "Изменения цен": {"A": 24,"B": 14,"C": 12,"D": 42,"E": 36,"F": 18,"G": 18,"H": 16,"I": 16,"J": 18,"K": 18,"L": 24},
        "Прогресс категорий": {"A": 14,"B": 40,"C": 16,"D": 22,"E": 16,"F": 16,"G": 24,"H": 12,"I": 12},
        "Экспорты": {"A": 24,"B": 14,"C": 40,"D": 12,"E": 16,"F": 54,"G": 64,"H": 68},
        "Сводка": {"A": 42,"B": 100},
    }
    price_headers = {"цена сейчас, ₽","обычная цена, ₽","цена по акции, ₽","предыдущая цена, ₽","новая цена, ₽","изменение, ₽"}
    for ws in wb.worksheets:
        ws.freeze_panes = "A2" if ws.max_row > 1 else None
        if ws.max_row > 0:
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.row_dimensions[1].height = 32
            for col, width in widths.get(ws.title, {}).items():
                ws.column_dimensions[col].width = width
            headers = {str(c.value): c.column for c in ws[1] if c.value is not None}
            for h, col in headers.items():
                if h in price_headers:
                    for r in range(2, ws.max_row + 1):
                        ws.cell(r, col).number_format = '#,##0.00 "₽"'
                if h in {"скидка, %", "изменение, %", "предыдущая скидка, %", "новая скидка, %"}:
                    for r in range(2, ws.max_row + 1):
                        ws.cell(r, col).number_format = '0.0"%"'
                if "URL" in h or h in {"карточка товара", "страница каталога", "исходная страница"}:
                    for r in range(2, ws.max_row + 1):
                        cell = ws.cell(r, col)
                        if isinstance(cell.value, str) and cell.value.startswith("https://"):
                            cell.hyperlink = cell.value
                            cell.font = Font(color="0563C1", underline="single")
            if ws.title in table_names and ws.max_row >= 2 and ws.max_column >= 1:
                table = Table(displayName=table_names[ws.title], ref=ws.dimensions)
                table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium4", showRowStripes=True, showColumnStripes=False)
                ws.add_table(table)
        if ws.title == "Сводка":
            for row in range(2, ws.max_row + 1):
                ws.cell(row, 1).font = Font(bold=True)
                ws.cell(row, 2).alignment = Alignment(vertical="top", wrap_text=True)
    wb.save(path)


def export_outputs(db: CatalogDB, expected_sap: str) -> dict[str, Any]:
    products = db.products()
    history = db.history()
    changes = db.changes()
    exports = db.exports()
    progress = db.progress(products)

    products_df = pd.DataFrame(products, columns=PRODUCT_COLUMNS)
    history_df = pd.DataFrame(history, columns=HISTORY_COLUMNS)
    changes_df = pd.DataFrame(changes, columns=CHANGE_COLUMNS)
    progress_df = pd.DataFrame(progress, columns=PROGRESS_COLUMNS)
    exports_df = pd.DataFrame(exports, columns=EXPORT_COLUMNS)
    summary_df = make_summary(products, history, changes, exports, expected_sap)

    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as writer:
        products_df.to_excel(writer, index=False, sheet_name="Товары")
        summary_df.to_excel(writer, index=False, sheet_name="Сводка")
        history_df.to_excel(writer, index=False, sheet_name="История цен")
        changes_df.to_excel(writer, index=False, sheet_name="Изменения цен")
        progress_df.to_excel(writer, index=False, sheet_name="Прогресс категорий")
        exports_df.to_excel(writer, index=False, sheet_name="Экспорты")
    products_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    style_workbook(OUTPUT_XLSX)

    sap_codes = sorted({str(p.get("SAP магазина")) for p in products if p.get("SAP магазина") and p.get("SAP магазина") != "НЕ УКАЗАН"})
    report = {
        "generated_at": utc_now(), "database": str(DB_PATH.name), "expected_sap_code": expected_sap,
        "store_sap_codes": sap_codes, "sap_mismatch": bool(sap_codes and any(s != expected_sap for s in sap_codes)),
        "products": len(products), "products_with_price": sum(p.get("цена сейчас, ₽") is not None for p in products),
        "products_with_discount": sum(p.get("скидка, %") is not None for p in products),
        "products_with_image_url": sum(bool(p.get("фото URL")) for p in products),
        "products_with_local_image": sum(bool(p.get("фото файл")) for p in products),
        "price_observations": len(history), "price_changes": len(changes),
        "source_exports": len(exports), "category_progress_rows": len(progress),
        "price_scope": "Онлайн-каталог 5ka.ru для выбранного адреса доставки; не гарантированная цена на полке.",
        "outputs": {"xlsx": OUTPUT_XLSX.name, "csv": OUTPUT_CSV.name, "database": DB_PATH.name},
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Накопительная база каталога Пятёрочки: SQLite + история цен")
    parser.add_argument("--no-photos", action="store_true", help="не скачивать фото; сохранить URL")
    parser.add_argument("--photo-delay", type=float, default=0.35, help="пауза между скачиваниями фото, секунды")
    parser.add_argument("--expected-sap", default="35XY", help="ожидаемый SAP выбранного магазина")
    parser.add_argument("--no-checkpoint", action="store_true", help="не импортировать старый checkpoint v4-v6")
    args = parser.parse_args()

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    db = CatalogDB(DB_PATH)
    newly_imported = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    source_files = sorted(p for p in EXPORT_DIR.rglob("*.json") if p.is_file())
    for path in source_files:
        try:
            source_hash = sha256_file(path)
            if db.has_export(source_hash):
                skipped += 1
                continue
            rows, meta = load_json_rows(path)
            if meta.get("is_checklist") or not rows:
                continue
            added, unique = db.import_rows(source_hash, path.name, rows, meta)
            newly_imported += 1
            print(f"Импорт: {path.name}: строк {added}, уникальных SAP+PLU {unique}")
            if meta.get("sap_code") and meta["sap_code"] != args.expected_sap:
                print(f"  ВНИМАНИЕ: в файле SAP {meta['sap_code']}, ожидался {args.expected_sap}")
            elif not meta.get("sap_code"):
                print("  ВНИМАНИЕ: SAP не определён в файле; цены нельзя уверенно привязать к магазину")
        except Exception as exc:
            errors.append({"file": str(path), "error": str(exc)})
            print(f"ОШИБКА {path.name}: {exc}")

    # Import legacy v4-v6 checkpoint exactly once by file fingerprint.
    if not args.no_checkpoint and CHECKPOINT_PATH.exists():
        try:
            source_hash = sha256_file(CHECKPOINT_PATH)
            if not db.has_export(source_hash):
                rows = load_checkpoint_rows(CHECKPOINT_PATH)
                meta = {
                    "category": "Legacy checkpoint v4-v6", "source_url": None,
                    "sap_code": "35XY", "captured_at": datetime.fromtimestamp(CHECKPOINT_PATH.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                }
                added, unique = db.import_rows(source_hash, CHECKPOINT_PATH.name, rows, meta)
                newly_imported += 1
                print(f"Checkpoint: строк {added}, уникальных SAP+PLU {unique}")
        except Exception as exc:
            errors.append({"file": str(CHECKPOINT_PATH), "error": str(exc)})
            print(f"ОШИБКА checkpoint: {exc}")

    # Import optional CSV/XLSX legacy tables once; use only fields understood here.
    if LEGACY_DIR.exists():
        for path in sorted(p for p in LEGACY_DIR.rglob("*") if p.is_file() and p.suffix.lower() in {".csv", ".xlsx", ".xls"}):
            try:
                source_hash = sha256_file(path)
                if db.has_export(source_hash):
                    skipped += 1
                    continue
                frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig") if path.suffix.lower() == ".csv" else pd.read_excel(path, dtype=str)
                aliases = {"SAP магазина": {"sap", "sap магазина"}, "PLU": {"plu", "id (plu)"}, "название": {"название", "наименование", "name"}, "цена сейчас, ₽": {"цена", "цена сейчас", "цена, ₽"}, "обычная цена, ₽": {"обычная цена", "старая цена"}, "фото URL": {"фото", "фото url", "image url"}, "категории": {"категория", "category"}}
                reverse = {a: k for k, vals in aliases.items() for a in vals}
                rows=[]
                for item in frame.to_dict(orient="records"):
                    row={}
                    for k,v in item.items():
                        target=reverse.get(str(k).strip().lower())
                        if target and v is not None and str(v).strip(): row[target]=v
                    if row.get("PLU"):
                        for key in ("цена сейчас, ₽", "обычная цена, ₽"):
                            row[key]=to_price(row.get(key))
                        row["цена по акции, ₽"]=None
                        row["скидка, %"] = None
                        row["время получения"]=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
                        rows.append(row)
                meta={"category":path.stem,"source_url":None,"sap_code":None,"captured_at":datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
                db.import_rows(source_hash,path.name,rows,meta)
                newly_imported += 1
                print(f"Legacy {path.name}: импортировано {len(rows)} строк")
            except Exception as exc:
                errors.append({"file": str(path), "error": str(exc)})
                print(f"ОШИБКА {path.name}: {exc}")

    products = db.products()
    if not products:
        print(f"Нет данных для базы. Положите выгрузки JSON в {EXPORT_DIR}")
        db.close()
        return 1

    if not args.no_photos:
        print(f"Проверка локальных фотографий: {sum(bool(p.get('фото URL')) for p in products)} URL...")
        for idx, product in enumerate(products, 1):
            image_file = product.get("фото файл")
            if image_file and (BASE_DIR / image_file).exists():
                pass
            elif product.get("фото URL"):
                saved = save_image(product.get("фото URL"), str(product.get("SAP магазина") or "store"), str(product.get("PLU")), max(args.photo_delay, 0.0))
                if saved:
                    db.update_image(str(product.get("SAP магазина")), str(product.get("PLU")), saved)
            if idx % 100 == 0:
                print(f"  проверено товаров: {idx}/{len(products)}")
    else:
        print("Скачивание фото отключено; URL сохранены в базе.")

    report = export_outputs(db, args.expected_sap)
    db.close()
    print("\nГОТОВО — база обновлена")
    print(f"Новых файлов импортировано: {newly_imported}; уже обработанных пропущено: {skipped}")
    print(f"Товаров в базе: {report['products']}; с ценой: {report['products_with_price']}; со скидкой: {report['products_with_discount']}")
    print(f"URL фото: {report['products_with_image_url']}; локальные фото: {report['products_with_local_image']}")
    print(f"Наблюдений цен: {report['price_observations']}; изменений цены/скидки: {report['price_changes']}")
    print(f"Файлов выгрузки: {report['source_exports']}; категорий в отчёте: {report['category_progress_rows']}")
    if report["sap_mismatch"]:
        print(f"ВНИМАНИЕ: найдены SAP {', '.join(report['store_sap_codes'])}, ожидался {args.expected_sap}")
    if errors:
        print(f"Ошибок импорта файлов: {len(errors)} — см. {REPORT_JSON.name}")
    print(f"Excel: {OUTPUT_XLSX}")
    print(f"CSV: {OUTPUT_CSV}")
    print(f"SQLite: {DB_PATH}")
    print(f"Отчёт: {REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
