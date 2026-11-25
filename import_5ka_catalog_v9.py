# -*- coding: utf-8 -*-
"""Накопительный импортёр каталога: SQLite + Excel + история наблюдений (v10).

Работает только с файлами, которые пользователь экспортировал из видимой страницы
магазина. Не открывает сайт, не использует cookies и не обращается к API каталога.

Запуск из папки проекта:
    python import_5ka_catalog_v9.py --chain 5ka
    python import_5ka_catalog_v9.py --chain perekrestok --input-dir <папка JSON>
    python import_5ka_catalog_v9.py --chain 5ka --no-photos

Новые JSON-файлы помещайте в 5ka_exports/. V9 мигрирует копию V8 SQLite, оригинал оставляет нетронутым. Повторный запуск безопасен:
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

from catalog_config import (
    DEFAULT_CHAIN,
    allowed_sap,
    chain_from_payload,
    get_store,
    normalize_chain,
    validate_chain_url,
)
from catalog_migrations import migrate_v9_to_v10

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
DB_PATH_V8 = BASE_DIR / "pyaterochka_catalog_v8.sqlite3"
DB_PATH = BASE_DIR / "pyaterochka_catalog_v9.sqlite3"
OUTPUT_XLSX = BASE_DIR / "pyaterochka_catalog_v9.xlsx"
OUTPUT_CSV = BASE_DIR / "pyaterochka_catalog_v9.csv"
REPORT_JSON = BASE_DIR / "pyaterochka_catalog_v9_report.json"
PHOTO_DIR = BASE_DIR / "photos5ka"

PRODUCT_COLUMNS = [
    "Сеть", "SAP магазина", "PLU", "название", "цена сейчас, ₽", "обычная цена, ₽",
    "цена по акции, ₽", "скидка, %", "маркер акции", "категории",
    "фото URL", "фото файл", "карточка товара",
    "дата первого появления (UTC)", "дата последнего наблюдения (UTC)",
    "дата первого импорта (UTC)", "дата последнего импорта (UTC)", "наблюдений цены",
]
HISTORY_COLUMNS = [
    "Сеть", "дата наблюдения (UTC)", "дата импорта (UTC)", "SAP магазина", "PLU",
    "название на момент выгрузки", "категория", "цена сейчас, ₽", "обычная цена, ₽",
    "цена по акции, ₽", "скидка, %", "маркер акции", "фото URL на момент выгрузки",
    "ссылка карточки на момент выгрузки", "страница каталога", "файл выгрузки",
]
CHANGE_COLUMNS = [
    "Сеть", "дата предыдущего наблюдения (UTC)", "дата изменения (UTC)", "дата импорта (UTC)",
    "SAP магазина", "PLU", "название", "категория",
    "предыдущая цена, ₽", "новая цена, ₽", "изменение, ₽", "изменение, %",
    "предыдущая обычная цена, ₽", "новая обычная цена, ₽",
    "предыдущая цена по акции, ₽", "новая цена по акции, ₽",
    "предыдущая скидка, %", "новая скидка, %", "что изменилось",
]
EVENT_COLUMNS = [
    "Сеть", "тип события", "дата наблюдения (UTC)", "дата импорта (UTC)",
    "предыдущее наблюдение (UTC)", "SAP магазина", "PLU", "название",
    "категория", "изменения", "цена сейчас, ₽", "обычная цена, ₽",
    "цена по акции, ₽", "скидка, %", "фото URL",
]
PROGRESS_COLUMNS = [
    "Сеть", "SAP магазина", "категория", "страниц выгружено", "номера страниц",
    "файлов выгрузки", "уникальных PLU", "первая выгрузка (UTC)",
    "последняя выгрузка (UTC)", "цены", "фото URL",
]
EXPORT_COLUMNS = [
    "Сеть", "дата наблюдения (UTC)", "дата импорта (UTC)", "SAP магазина", "категория", "страница",
    "товаров в файле", "имя файла", "исходная страница", "SHA-256",
]
DETAIL_COLUMNS = [
    "Сеть", "SAP магазина", "PLU", "название", "описание", "состав", "пищевая ценность JSON",
    "характеристики JSON", "полный текст карточки", "ссылка товара", "фото URL",
    "дата первого импорта доп. сведений (UTC)", "дата последнего импорта доп. сведений (UTC)",
    "дата наблюдения доп. сведений (UTC)",
]
IMAGE_COLUMNS = ["Сеть", "SAP магазина", "PLU", "порядок фото", "URL фото", "локальный файл", "источник", "первое появление фото (UTC)", "последнее появление фото (UTC)"]

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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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


def detect_chain(payload: dict[str, Any] | None, default: str = DEFAULT_CHAIN) -> str:
    """Определить сеть выгрузки (общая логика лежит в catalog_config)."""
    return chain_from_payload(payload) if isinstance(payload, dict) else default


def load_json_rows(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    chain = detect_chain(payload)
    if isinstance(payload, dict) and str(payload.get("source") or "").endswith("visible-category-links"):
        return [], {"is_checklist": True}
    rows: list[dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        products = payload["products"]
        is_visible = str(payload.get("source") or "").endswith("visible-page-export") or any(
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
    for row in rows:
        row["Сеть"] = chain
    meta = {
        "chain": chain,
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
                "Сеть": DEFAULT_CHAIN, "SAP магазина": "35XY", "PLU": str(plu), "название": item.get("название") or item.get("name"),
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
    """SQLite database with a clear distinction between observed_at and imported_at."""

    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _table_columns(self, table: str) -> set[str]:
        return {str(r["name"]) for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _add_column_if_missing(self, table: str, column: str, declaration: str) -> None:
        if column not in self._table_columns(table):
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

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
            first_imported_at TEXT,
            last_imported_at TEXT,
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
            imported_at TEXT,
            product_name TEXT,
            image_url TEXT,
            product_url TEXT,
            UNIQUE (source_hash, sap_code, plu),
            FOREIGN KEY (source_hash) REFERENCES exports(source_hash)
        );
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_history_product_time ON price_history(sap_code, plu, captured_at);
        CREATE INDEX IF NOT EXISTS idx_history_time ON price_history(captured_at);
        """)

        # Safe, idempotent migration for databases copied from V8.
        self._add_column_if_missing("products", "first_imported_at", "TEXT")
        self._add_column_if_missing("products", "last_imported_at", "TEXT")
        self._add_column_if_missing("price_history", "imported_at", "TEXT")
        self._add_column_if_missing("price_history", "product_name", "TEXT")
        self._add_column_if_missing("price_history", "image_url", "TEXT")
        self._add_column_if_missing("price_history", "product_url", "TEXT")

        # Backfill old V8 history with real file-import dates from the exports table.
        # Historical product names/images are not present in V8 price_history, so for
        # legacy rows only, their current known values are copied as a best-effort label.
        self.conn.execute("""
            UPDATE price_history
            SET imported_at = COALESCE(imported_at,
                (SELECT e.imported_at FROM exports e WHERE e.source_hash=price_history.source_hash),
                captured_at)
        """)
        self.conn.execute("""
            UPDATE price_history
            SET product_name = COALESCE(product_name,
                (SELECT p.name FROM products p WHERE p.sap_code=price_history.sap_code AND p.plu=price_history.plu)),
                image_url = COALESCE(image_url,
                (SELECT p.image_url FROM products p WHERE p.sap_code=price_history.sap_code AND p.plu=price_history.plu)),
                product_url = COALESCE(product_url,
                (SELECT p.product_url FROM products p WHERE p.sap_code=price_history.sap_code AND p.plu=price_history.plu))
        """)
        self.conn.execute("""
            UPDATE products
            SET first_imported_at = COALESCE(first_imported_at,
                (SELECT MIN(h.imported_at) FROM price_history h WHERE h.sap_code=products.sap_code AND h.plu=products.plu),
                first_seen_at),
                last_imported_at = COALESCE(last_imported_at,
                (SELECT MAX(h.imported_at) FROM price_history h WHERE h.sap_code=products.sap_code AND h.plu=products.plu),
                last_seen_at)
        """)
        self.conn.execute("INSERT INTO schema_meta(key,value) VALUES('schema_version','9') ON CONFLICT(key) DO UPDATE SET value='9'")
        self.conn.commit()
        # V10: добавляем chain и расширяем ключи; идемпотентно.
        migrate_v9_to_v10(self.conn)

    def has_export(self, source_hash: str) -> bool:
        return self.conn.execute("SELECT 1 FROM exports WHERE source_hash=?", (source_hash,)).fetchone() is not None

    def import_rows(self, source_hash: str, file_name: str, rows: list[dict[str, Any]], meta: dict[str, Any]) -> tuple[int, int]:
        if self.has_export(source_hash):
            return 0, 0

        captured = normalize_timestamp(meta.get("captured_at"), utc_now())
        imported = utc_now()  # actual time this file is inserted into our local database
        chain = normalize_chain(meta.get("chain") or DEFAULT_CHAIN)
        validate_chain_url(chain, meta.get("source_url"))
        allowed = allowed_sap(chain)
        sap_from_meta = str(meta.get("sap_code") or "").strip()
        if sap_from_meta and sap_from_meta not in allowed:
            raise ValueError(
                f"SAP {sap_from_meta} не относится к сети {chain}; допустимо: {', '.join(allowed)}"
            )
        unique_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            plu = str(row.get("PLU") or "").strip()
            if not plu:
                continue
            row_chain = normalize_chain(row.get("Сеть") or chain)
            if row_chain != chain:
                raise ValueError(
                    f"В одной выгрузке смешаны сети: метаданные={chain}, строка {plu}={row_chain}"
                )
            sap_value = first_present(row.get("SAP магазина"), sap_from_meta, get_store(row_chain)["default_sap"])
            sap = str(sap_value or "").strip()
            if sap.casefold() in {"", "не указан", "не указана", "unknown", "null", "none"}:
                sap = str(sap_from_meta or get_store(row_chain)["default_sap"]).strip()
            if sap not in allowed_sap(row_chain):
                raise ValueError(
                    f"SAP {sap} не относится к сети {row_chain}; допустимо: {', '.join(allowed_sap(row_chain))}"
                )
            if sap_from_meta and sap != sap_from_meta:
                raise ValueError(
                    f"SAP строки {sap} не совпадает с SAP выгрузки {sap_from_meta}"
                )
            validate_chain_url(row_chain, row.get("карточка товара"))
            normalized = dict(row)
            normalized["PLU"] = plu
            normalized["Сеть"] = row_chain
            normalized["SAP магазина"] = sap
            normalized["время получения"] = normalize_timestamp(row.get("время получения"), captured)
            unique_rows[(row_chain, sap, plu)] = normalized

        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """INSERT INTO exports(chain,source_hash,file_name,category,source_url,sap_code,captured_at,imported_at,product_rows,unique_products,page_number)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (chain, source_hash, file_name, meta.get("category"), meta.get("source_url"), sap_from_meta,
                 captured, imported, len(rows), len(unique_rows), page_number(meta.get("source_url"))),
            )

            for (row_chain, sap, plu), row in unique_rows.items():
                seen_at = normalize_timestamp(row.get("время получения"), captured)
                category = str(row.get("категории") or meta.get("category") or "не указана").strip()
                current = row.get("цена сейчас, ₽")
                regular = row.get("обычная цена, ₽")
                promo = row.get("цена по акции, ₽")
                discount_pct = row.get("скидка, %")
                existing = self.conn.execute(
                    """SELECT first_seen_at,last_seen_at,first_imported_at,last_imported_at
                       FROM products WHERE chain=? AND sap_code=? AND plu=?""", (row_chain, sap, plu)
                ).fetchone()

                if existing is None:
                    self.conn.execute(
                        """INSERT INTO products(
                            chain,sap_code,plu,name,current_price,regular_price,promo_price,discount_pct,promo_label,
                            categories,image_url,image_file,product_url,first_seen_at,last_seen_at,last_source_hash,
                            first_imported_at,last_imported_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row_chain, sap, plu, row.get("название"), current, regular, promo, discount_pct, row.get("маркер акции"),
                         category, row.get("фото URL"), None, row.get("карточка товара"), seen_at, seen_at,
                         source_hash, imported, imported),
                    )
                else:
                    first_seen = min(str(existing["first_seen_at"]), seen_at)
                    last_seen = max(str(existing["last_seen_at"]), seen_at)
                    first_imported = min(str(existing["first_imported_at"] or imported), imported)
                    last_imported = max(str(existing["last_imported_at"] or imported), imported)
                    is_newer_observation = seen_at >= str(existing["last_seen_at"])
                    if is_newer_observation:
                        self.conn.execute(
                            """UPDATE products SET
                                name=COALESCE(?,name), current_price=?,
                                regular_price=?, promo_price=?, discount_pct=?, promo_label=? ,
                                image_url=COALESCE(?,image_url), product_url=COALESCE(?,product_url),
                                first_seen_at=?,last_seen_at=?,last_source_hash=?,
                                first_imported_at=?,last_imported_at=?
                                WHERE chain=? AND sap_code=? AND plu=?""",
                            (row.get("название"), current, regular, promo, discount_pct, row.get("маркер акции"),
                             row.get("фото URL"), row.get("карточка товара"), first_seen, last_seen, source_hash,
                             first_imported, last_imported, row_chain, sap, plu),
                        )
                    else:
                        self.conn.execute(
                            """UPDATE products SET first_seen_at=?,last_seen_at=?,
                               first_imported_at=?,last_imported_at=? WHERE chain=? AND sap_code=? AND plu=?""",
                            (first_seen, last_seen, first_imported, last_imported, row_chain, sap, plu),
                        )

                self.conn.execute(
                    """INSERT INTO product_categories(chain,sap_code,plu,category,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(chain,sap_code,plu,category) DO UPDATE SET
                       last_seen_at=MAX(last_seen_at,excluded.last_seen_at),
                       first_seen_at=MIN(first_seen_at,excluded.first_seen_at)""",
                    (row_chain, sap, plu, category, seen_at, seen_at),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO price_history(
                        chain,source_hash,sap_code,plu,category,captured_at,current_price,regular_price,promo_price,
                        discount_pct,promo_label,raw_text,source_url,file_name,imported_at,product_name,image_url,product_url
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row_chain, source_hash, sap, plu, category, seen_at, current, regular, promo, discount_pct,
                     row.get("маркер акции"), row.get("сырой текст карточки"), meta.get("source_url"),
                     file_name, imported, row.get("название"), row.get("фото URL"), row.get("карточка товара")),
                )

            for row_chain, sap, plu in unique_rows:
                cats = [r[0] for r in self.conn.execute(
                    "SELECT category FROM product_categories WHERE chain=? AND sap_code=? AND plu=? ORDER BY category",
                    (row_chain, sap, plu)
                ).fetchall()]
                self.conn.execute(
                    "UPDATE products SET categories=? WHERE chain=? AND sap_code=? AND plu=?",
                    (" | ".join(cats), row_chain, sap, plu),
                )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(rows), len(unique_rows)

    def update_image(self, chain: str, sap: str, plu: str, image_file: str) -> None:
        self.conn.execute(
            "UPDATE products SET image_file=? WHERE chain=? AND sap_code=? AND plu=?",
            (image_file, normalize_chain(chain), sap, plu),
        )
        self.conn.commit()

    def products(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM price_history h
                         WHERE h.chain=p.chain AND h.sap_code=p.sap_code AND h.plu=p.plu) AS observation_count
            FROM products p ORDER BY p.chain,p.sap_code,p.categories,p.name
        """).fetchall()
        return [{
            "Сеть": r["chain"], "SAP магазина": r["sap_code"], "PLU": r["plu"], "название": r["name"],
            "цена сейчас, ₽": r["current_price"], "обычная цена, ₽": r["regular_price"],
            "цена по акции, ₽": r["promo_price"], "скидка, %": r["discount_pct"],
            "маркер акции": r["promo_label"], "категории": r["categories"],
            "фото URL": r["image_url"], "фото файл": r["image_file"],
            "карточка товара": r["product_url"], "дата первого появления (UTC)": r["first_seen_at"],
            "дата последнего наблюдения (UTC)": r["last_seen_at"],
            "дата первого импорта (UTC)": r["first_imported_at"],
            "дата последнего импорта (UTC)": r["last_imported_at"],
            "наблюдений цены": r["observation_count"],
        } for r in rows]

    def history(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT h.chain,h.captured_at,h.imported_at,h.sap_code,h.plu,h.product_name,h.category,
                   h.current_price,h.regular_price,h.promo_price,h.discount_pct,h.promo_label,
                   h.image_url,h.product_url,h.source_url,h.file_name
            FROM price_history h
            ORDER BY h.captured_at,h.imported_at,h.sap_code,h.plu,h.id
        """).fetchall()
        keys = HISTORY_COLUMNS
        return [dict(zip(keys, tuple(r))) for r in rows]

    @staticmethod
    def _different(old: Any, new: Any) -> bool:
        if old is None and new is None:
            return False
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            return round(float(old), 4) != round(float(new), 4)
        return old != new

    def changes(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT h.id,h.chain,h.captured_at,h.imported_at,h.sap_code,h.plu,h.product_name,h.category,
                   h.current_price,h.regular_price,h.promo_price,h.discount_pct,h.promo_label
            FROM price_history h
            ORDER BY h.chain,h.sap_code,h.plu,h.captured_at,h.imported_at,h.id
        """).fetchall()
        previous: dict[tuple[str, str, str], sqlite3.Row] = {}
        output: list[dict[str, Any]] = []
        fields = [
            ("current_price", "текущая цена"), ("regular_price", "обычная цена"),
            ("promo_price", "цена по акции"), ("discount_pct", "процент скидки"),
            ("promo_label", "условие/маркер акции"),
        ]
        for r in rows:
            key = (r["chain"], r["sap_code"], r["plu"])
            old = previous.get(key)
            previous[key] = r
            if old is None:
                continue
            changed = [label for col, label in fields if self._different(old[col], r[col])]
            if not changed:
                continue
            old_price, new_price = old["current_price"], r["current_price"]
            delta = round(float(new_price) - float(old_price), 2) if old_price is not None and new_price is not None else None
            delta_pct = round(delta / float(old_price) * 100, 2) if delta is not None and old_price else None
            output.append({
                "дата предыдущего наблюдения (UTC)": old["captured_at"],
                "дата изменения (UTC)": r["captured_at"], "дата импорта (UTC)": r["imported_at"],
                "SAP магазина": r["sap_code"], "PLU": r["plu"], "название": r["product_name"],
                "Сеть": r["chain"], "категория": r["category"], "предыдущая цена, ₽": old_price,
                "новая цена, ₽": new_price, "изменение, ₽": delta, "изменение, %": delta_pct,
                "предыдущая обычная цена, ₽": old["regular_price"], "новая обычная цена, ₽": r["regular_price"],
                "предыдущая цена по акции, ₽": old["promo_price"], "новая цена по акции, ₽": r["promo_price"],
                "предыдущая скидка, %": old["discount_pct"], "новая скидка, %": r["discount_pct"],
                "что изменилось": ", ".join(changed),
            })
        return output

    def events(self) -> list[dict[str, Any]]:
        """First appearance + changes in price and visible card fields."""
        rows = self.conn.execute("""
            SELECT h.id,h.chain,h.captured_at,h.imported_at,h.sap_code,h.plu,h.product_name,h.category,
                   h.current_price,h.regular_price,h.promo_price,h.discount_pct,h.promo_label,
                   h.image_url,h.product_url
            FROM price_history h
            ORDER BY h.chain,h.sap_code,h.plu,h.captured_at,h.imported_at,h.id
        """).fetchall()
        prev: dict[tuple[str, str, str], sqlite3.Row] = {}
        events: list[dict[str, Any]] = []
        field_labels = [
            ("current_price", "текущая цена"), ("regular_price", "обычная цена"),
            ("promo_price", "цена по акции"), ("discount_pct", "скидка"),
            ("promo_label", "условия акции"), ("product_name", "название"),
            ("image_url", "фото"), ("product_url", "ссылка карточки"), ("category", "категория"),
        ]
        for r in rows:
            key = (r["chain"], r["sap_code"], r["plu"])
            old = prev.get(key)
            prev[key] = r
            if old is None:
                event_type = "Первое появление"
                differences = "Товар впервые попал в импортированную выгрузку"
                previous_at = None
            else:
                differences = "; ".join(label for col, label in field_labels if self._different(old[col], r[col]))
                if not differences:
                    continue
                event_type = "Изменение карточки/цены"
                previous_at = old["captured_at"]
            events.append({
                "тип события": event_type, "дата наблюдения (UTC)": r["captured_at"],
                "дата импорта (UTC)": r["imported_at"], "предыдущее наблюдение (UTC)": previous_at,
                "SAP магазина": r["sap_code"], "PLU": r["plu"], "название": r["product_name"],
                "Сеть": r["chain"], "категория": r["category"], "изменения": differences,
                "цена сейчас, ₽": r["current_price"], "обычная цена, ₽": r["regular_price"],
                "цена по акции, ₽": r["promo_price"], "скидка, %": r["discount_pct"],
                "фото URL": r["image_url"],
            })
        events.sort(key=lambda x: (x["дата наблюдения (UTC)"], x["Сеть"], x["SAP магазина"], x["PLU"]))
        return events

    def exports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT chain,captured_at,imported_at,sap_code,category,page_number,product_rows,file_name,source_url,source_hash
            FROM exports ORDER BY captured_at,imported_at
        """).fetchall()
        return [dict(zip(EXPORT_COLUMNS, tuple(r))) for r in rows]

    def progress(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        exports = self.exports()
        by_cat: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for e in exports:
            chain = str(e.get("Сеть") or DEFAULT_CHAIN)
            by_cat[(chain, str(e.get("SAP магазина") or "НЕ УКАЗАН"), str(e.get("категория") or "не указана"))].append(e)
        product_map: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for p in products:
            chain = str(p.get("Сеть") or DEFAULT_CHAIN)
            sap = str(p.get("SAP магазина") or "НЕ УКАЗАН")
            for cat in str(p.get("категории") or "не указана").split(" | "):
                product_map[(chain, sap, cat)].append(p)
        output = []
        for key, files in sorted(by_cat.items()):
            items = product_map.get(key, [])
            pages = sorted({str(e["страница"]) for e in files if e.get("страница") is not None},
                           key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else x))
            output.append({
                "Сеть": key[0], "SAP магазина": key[1], "категория": key[2], "страниц выгружено": len(pages) if pages else None,
                "номера страниц": ", ".join(pages) if pages else "не определено",
                "файлов выгрузки": len(files), "уникальных PLU": len({p["PLU"] for p in items}),
                "первая выгрузка (UTC)": min(str(e.get("дата наблюдения (UTC)") or "") for e in files),
                "последняя выгрузка (UTC)": max(str(e.get("дата наблюдения (UTC)") or "") for e in files),
                "цены": sum(p.get("цена сейчас, ₽") is not None for p in items),
                "фото URL": sum(bool(p.get("фото URL")) for p in items),
            })
        return output

    def close(self) -> None:
        self.conn.close()


def save_image(url: str | None, chain: str, sap: str, plu: str, delay: float) -> str | None:
    if not url or not str(url).startswith("https://"):
        return None
    store = get_store(normalize_chain(chain))
    host = str(urlparse(url).hostname or "").lower()
    if not any(host.endswith(str(h).lower()) for h in store["image_hosts"]):
        return None
    photo_dir = BASE_DIR / store["paths"]["photos"]
    photo_dir.mkdir(parents=True, exist_ok=True)
    safe_plu = re.sub(r"[^A-Za-z0-9_-]", "_", str(plu or "unknown"))
    old_path = photo_dir / f"{safe_plu}.jpg"
    # Reuse the v7 filename when it is already present.
    if old_path.exists() and old_path.stat().st_size > 0:
        return str(old_path.relative_to(BASE_DIR))
    safe_sap = re.sub(r"[^A-Za-z0-9_-]", "_", str(sap or "store"))
    dest = photo_dir / f"{safe_sap}_{safe_plu}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return str(dest.relative_to(BASE_DIR))
    try:
        response = requests.get(url, timeout=25, headers={"Referer": store["referer"]})
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


def make_summary(products: list[dict[str, Any]], history: list[dict[str, Any]], changes: list[dict[str, Any]], exports: list[dict[str, Any]], expected_sap: str, chain: str = DEFAULT_CHAIN) -> pd.DataFrame:
    sap_codes = sorted({str(p.get("SAP магазина")) for p in products if p.get("SAP магазина") and p.get("SAP магазина") != "НЕ УКАЗАН"})
    first_seen = [str(p.get("дата первого появления (UTC)")) for p in products if p.get("дата первого появления (UTC)")]
    last_seen = [str(p.get("дата последнего наблюдения (UTC)")) for p in products if p.get("дата последнего наблюдения (UTC)")]
    first_imported = [str(p.get("дата первого импорта (UTC)")) for p in products if p.get("дата первого импорта (UTC)")]
    last_imported = [str(p.get("дата последнего импорта (UTC)")) for p in products if p.get("дата последнего импорта (UTC)")]
    metrics = [
        ("Версия схемы базы", "10"),
        ("Сеть", get_store(normalize_chain(chain))["title"]),
        ("Магазин SAP (ожидаемый)", expected_sap),
        ("Фактически найденные SAP", ", ".join(sap_codes) if sap_codes else "не указаны"),
        ("Товаров в текущей базе", len(products)),
        ("Товаров с текущей ценой", sum(p.get("цена сейчас, ₽") is not None for p in products)),
        ("Товаров со скидкой", sum(p.get("скидка, %") is not None for p in products)),
        ("Товаров с URL фото", sum(bool(p.get("фото URL")) for p in products)),
        ("Локальных фото", sum(bool(p.get("фото файл")) for p in products)),
        ("Наблюдений цены в истории", len(history)),
        ("Зафиксированных изменений цены/скидки", len(changes)),
        ("Событий появления/изменений карточки", None),
        ("Импортированных файлов выгрузки", len(exports)),
        ("Самая ранняя дата появления товара (UTC)", min(first_seen) if first_seen else None),
        ("Самая поздняя дата наблюдения товара (UTC)", max(last_seen) if last_seen else None),
        ("Первый импорт в базу (UTC)", min(first_imported) if first_imported else None),
        ("Последний импорт в базу (UTC)", max(last_imported) if last_imported else None),
        ("Дата формирования отчёта (UTC)", utc_now()),
        ("Важное замечание", "Дата появления = первое наблюдение в выгрузке; дата импорта = фактическая загрузка JSON в SQLite. Время хранится в UTC."),
        ("Область цен", f"Онлайн-каталог {get_store(normalize_chain(chain))['domain']} для выбранного адреса доставки; цена может отличаться от цены на полке."),
    ]
    return pd.DataFrame(metrics, columns=["Показатель", "Значение"])


def style_workbook(path: Path) -> None:
    wb = load_workbook(path)
    header_fill = PatternFill("solid", fgColor="166534")
    header_font = Font(bold=True, color="FFFFFF")
    table_names = {
        "Товары": "CurrentProductsV9", "История цен": "PriceHistoryV9",
        "Изменения цен": "PriceChangesV9", "События карточек": "ProductEventsV9",
        "Прогресс категорий": "CategoryProgressV9", "Экспорты": "CatalogExportsV9",
        "Подробные сведения": "ProductDetailsV11",
        "Фотогалерея": "ProductImagesV12",
    }
    widths = {
        "Товары": {"A":14,"B":12,"C":48,"D":16,"E":16,"F":16,"G":12,"H":22,"I":42,"J":50,"K":32,"L":50,"M":25,"N":25,"O":25,"P":25,"Q":18},
        "История цен": {"A":25,"B":25,"C":14,"D":12,"E":48,"F":38,"G":16,"H":16,"I":16,"J":12,"K":22,"L":50,"M":50,"N":62,"O":52},
        "Изменения цен": {"A":25,"B":25,"C":25,"D":14,"E":12,"F":44,"G":38,"H":18,"I":18,"J":16,"K":16,"L":20,"M":20,"N":20,"O":20,"P":18,"Q":18,"R":34},
        "События карточек": {"A":28,"B":25,"C":25,"D":25,"E":14,"F":12,"G":44,"H":38,"I":52,"J":16,"K":16,"L":16,"M":12,"N":54},
        "Прогресс категорий": {"A":14,"B":40,"C":16,"D":22,"E":16,"F":16,"G":25,"H":25,"I":12,"J":12},
        "Экспорты": {"A":25,"B":25,"C":14,"D":40,"E":12,"F":16,"G":54,"H":62,"I":68},
        "Подробные сведения": {"A":14,"B":12,"C":48,"D":48,"E":65,"F":40,"G":55,"H":70,"I":60,"J":55,"K":29,"L":29,"M":29},
        "Фотогалерея": {"A":14,"B":12,"C":14,"D":68,"E":42,"F":18,"G":28,"H":28},
        "Сводка": {"A":48,"B":100},
    }
    price_headers = {"цена сейчас, ₽","обычная цена, ₽","цена по акции, ₽","предыдущая цена, ₽","новая цена, ₽","изменение, ₽","предыдущая обычная цена, ₽","новая обычная цена, ₽","предыдущая цена по акции, ₽","новая цена по акции, ₽"}
    pct_headers = {"скидка, %","изменение, %","предыдущая скидка, %","новая скидка, %"}
    for ws in wb.worksheets:
        ws.freeze_panes = "A2" if ws.max_row > 1 else None
        if ws.max_row > 0:
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.row_dimensions[1].height = 40
            for col, width in widths.get(ws.title, {}).items():
                ws.column_dimensions[col].width = width
            headers = {str(c.value): c.column for c in ws[1] if c.value is not None}
            for h, col in headers.items():
                if h in price_headers:
                    for r in range(2, ws.max_row + 1):
                        ws.cell(r, col).number_format = '#,##0.00 "₽"'
                if h in pct_headers:
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


def export_outputs(db: CatalogDB, expected_sap: str, chain: str = DEFAULT_CHAIN) -> dict[str, Any]:
    chain = normalize_chain(chain)
    xlsx_path = OUTPUT_XLSX if chain == DEFAULT_CHAIN else BASE_DIR / f"catalog_{chain}_v10.xlsx"
    csv_path = OUTPUT_CSV if chain == DEFAULT_CHAIN else BASE_DIR / f"catalog_{chain}_v10.csv"
    report_path = REPORT_JSON if chain == DEFAULT_CHAIN else BASE_DIR / f"catalog_{chain}_v10_report.json"
    products = db.products()
    # Optional V11 enriched product details live in the same SQLite file.
    detail_rows = []
    try:
        raw_details = db.conn.execute("""SELECT chain,sap_code,plu,product_name,description,composition,nutrition_json,
            attributes_json,details_text,product_url,image_url,first_imported_at,last_imported_at,last_captured_at
            FROM product_details ORDER BY sap_code,product_name COLLATE NOCASE""").fetchall()
        detail_rows = [dict(zip(DETAIL_COLUMNS, tuple(r))) for r in raw_details]
    except sqlite3.OperationalError:
        detail_rows = []
    history = db.history()
    changes = db.changes()
    events = db.events()
    exports = db.exports()
    progress = db.progress(products)

    products_df = pd.DataFrame(products, columns=PRODUCT_COLUMNS)
    history_df = pd.DataFrame(history, columns=HISTORY_COLUMNS)
    changes_df = pd.DataFrame(changes, columns=CHANGE_COLUMNS)
    events_df = pd.DataFrame(events, columns=EVENT_COLUMNS)
    progress_df = pd.DataFrame(progress, columns=PROGRESS_COLUMNS)
    exports_df = pd.DataFrame(exports, columns=EXPORT_COLUMNS)
    details_df = pd.DataFrame(detail_rows, columns=DETAIL_COLUMNS)
    image_rows = []
    try:
        raw_images = db.conn.execute("""SELECT chain,sap_code,plu,image_order,image_url,local_file,source,first_seen_at,last_seen_at
            FROM product_images ORDER BY sap_code,plu,image_order,id""").fetchall()
        image_rows = [dict(zip(IMAGE_COLUMNS, tuple(r))) for r in raw_images]
    except sqlite3.OperationalError:
        image_rows = []
    images_df = pd.DataFrame(image_rows, columns=IMAGE_COLUMNS)
    summary_df = make_summary(products, history, changes, exports, expected_sap, chain)
    if len(summary_df) >= 2:
        summary_df.loc[summary_df["Показатель"] == "Событий появления/изменений карточки", "Значение"] = len(events)

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        products_df.to_excel(writer, index=False, sheet_name="Товары")
        summary_df.to_excel(writer, index=False, sheet_name="Сводка")
        history_df.to_excel(writer, index=False, sheet_name="История цен")
        changes_df.to_excel(writer, index=False, sheet_name="Изменения цен")
        events_df.to_excel(writer, index=False, sheet_name="События карточек")
        progress_df.to_excel(writer, index=False, sheet_name="Прогресс категорий")
        exports_df.to_excel(writer, index=False, sheet_name="Экспорты")
        details_df.to_excel(writer, index=False, sheet_name="Подробные сведения")
        images_df.to_excel(writer, index=False, sheet_name="Фотогалерея")
    products_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    style_workbook(xlsx_path)

    sap_codes = sorted({str(p.get("SAP магазина")) for p in products if p.get("SAP магазина") and p.get("SAP магазина") != "НЕ УКАЗАН"})
    report = {
        "generated_at_utc": utc_now(), "database": str(DB_PATH.name), "schema_version": 10,
        "chain": chain, "expected_sap_code": expected_sap, "store_sap_codes": sap_codes,
        "sap_mismatch": bool(sap_codes and any(s not in get_store(chain)["sap_codes"] for s in sap_codes)),
        "products": len(products), "products_with_price": sum(p.get("цена сейчас, ₽") is not None for p in products),
        "products_with_discount": sum(p.get("скидка, %") is not None for p in products),
        "products_with_image_url": sum(bool(p.get("фото URL")) for p in products),
        "products_with_local_image": sum(bool(p.get("фото файл")) for p in products),
        "price_observations": len(history), "price_changes": len(changes), "product_events": len(events),
        "source_exports": len(exports), "category_progress_rows": len(progress),
        "products_with_extended_details": len(detail_rows),
        "product_gallery_images": len(image_rows),
        "date_semantics": {
            "first_seen_at": "время снимка каталога, когда PLU впервые был замечен в выгрузке",
            "last_seen_at": "время самого позднего снимка каталога для PLU",
            "first_imported_at": "фактическое время первого импорта строки в эту базу",
            "last_imported_at": "фактическое время последнего импорта строки в эту базу",
            "all_timestamps_timezone": "UTC",
            "legacy_history_note": "Для наблюдений, импортированных из V8, дата импорта взята из exports.imported_at. Старые значения названия/фото в истории восстановить точно нельзя; в миграции они заполнены последними известными данными карточки.",
        },
        "price_scope": f"Онлайн-каталог {get_store(chain)['domain']} для выбранного адреса доставки; не гарантированная цена на полке.",
        "outputs": {
            "xlsx": xlsx_path.name, "csv": csv_path.name, "report": report_path.name,
            "database": DB_PATH.name,
        },
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def bootstrap_v9_database() -> bool:
    """Copy V8 SQLite into a new V9 file using SQLite backup API (WAL-safe)."""
    if DB_PATH.exists():
        return False
    if not DB_PATH_V8.exists():
        return False
    source = sqlite3.connect(str(DB_PATH_V8))
    target = sqlite3.connect(str(DB_PATH))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    print(f"Миграция: база V8 скопирована в {DB_PATH.name}; исходная база V8 сохранена без изменений.")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Каталог Пятёрочки: накопительная база и история изменений")
    parser.add_argument("--no-photos", action="store_true", help="не скачивать фото; сохранить URL")
    parser.add_argument("--photos-only", action="store_true", help="только дозагрузить отсутствующие фотографии из существующей базы")
    parser.add_argument("--photo-delay", type=float, default=0.35, help="пауза между скачиваниями фото, секунды")
    parser.add_argument("--chain", default=DEFAULT_CHAIN, help="сеть: 5ka или perekrestok")
    parser.add_argument("--expected-sap", default=None, help="устаревший алиас: ожидаемый SAP (для обратной совместимости)")
    parser.add_argument("--input-dir", type=Path, default=None, help="папка с JSON-выгрузками; по умолчанию — папка сети")
    parser.add_argument("--no-checkpoint", action="store_true", help="не импортировать старый checkpoint v4-v6")
    args = parser.parse_args()

    chain = normalize_chain(args.chain)
    allowed_saps = get_store(chain)["sap_codes"]
    expected_sap = args.expected_sap or allowed_saps[0]
    if expected_sap not in allowed_saps:
        parser.error(f"--expected-sap {expected_sap} не относится к {chain}; допустимо: {', '.join(allowed_saps)}")
    input_dir = args.input_dir or (BASE_DIR / get_store(chain)["paths"]["exports"])
    input_dir.mkdir(parents=True, exist_ok=True)

    migrated = bootstrap_v9_database()
    db = CatalogDB(DB_PATH)
    if migrated:
        print("Схема V9 добавлена: дата первого появления, дата импорта и снимки карточек.")
    newly_imported = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    if not args.photos_only:
        source_files = sorted(p for p in input_dir.rglob("*.json") if p.is_file())
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
                print(f"Импорт: {path.name}: строк {added}, уникальных товаров {unique}")
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
                        "chain": DEFAULT_CHAIN,
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


    else:
        print("Режим дозагрузки фото: новые JSON-выгрузки не импортируются.")

    products = db.products()
    if not products:
        print(f"Нет данных для базы. Положите выгрузки JSON в {input_dir}")
        db.close()
        return 1

    if not args.no_photos:
        print(f"Проверка локальных фотографий: {sum(bool(p.get('фото URL')) for p in products)} URL...")
        for idx, product in enumerate(products, 1):
            image_file = product.get("фото файл")
            if image_file and (BASE_DIR / image_file).exists():
                pass
            elif product.get("фото URL"):
                saved = save_image(
                    product.get("фото URL"), str(product.get("Сеть") or chain),
                    str(product.get("SAP магазина") or "store"), str(product.get("PLU")),
                    max(args.photo_delay, 0.0),
                )
                if saved:
                    db.update_image(
                        str(product.get("Сеть") or chain), str(product.get("SAP магазина")),
                        str(product.get("PLU")), saved,
                    )
            if idx % 100 == 0:
                print(f"  проверено товаров: {idx}/{len(products)}")
    else:
        print("Скачивание фото отключено; URL сохранены в базе.")

    report = export_outputs(db, expected_sap, chain=chain)
    db.close()
    print("\nГОТОВО — база обновлена")
    print(f"Новых файлов импортировано: {newly_imported}; уже обработанных пропущено: {skipped}")
    print(f"Товаров в базе: {report['products']}; с ценой: {report['products_with_price']}; со скидкой: {report['products_with_discount']}")
    print(f"URL фото: {report['products_with_image_url']}; локальные фото: {report['products_with_local_image']}")
    print(f"Наблюдений цен: {report['price_observations']}; изменений цены/скидки: {report['price_changes']}; событий карточек: {report['product_events']}")
    print(f"Файлов выгрузки: {report['source_exports']}; категорий в отчёте: {report['category_progress_rows']}")
    if report["sap_mismatch"]:
        print(f"ВНИМАНИЕ: найдены SAP {', '.join(report['store_sap_codes'])}, для сети {chain} ожидался один из {allowed_saps}")
    if errors:
        print(f"Ошибок импорта файлов: {len(errors)} — см. {report['outputs']['report']}")
    print("Время в базе и Excel: UTC (отдельно дата наблюдения и фактическая дата импорта).")
    print(f"Excel: {report['outputs']['xlsx']}")
    print(f"CSV: {report['outputs']['csv']}")
    print(f"SQLite: {DB_PATH}")
    print(f"Отчёт: {report['outputs']['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
