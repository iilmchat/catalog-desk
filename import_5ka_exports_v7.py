# -*- coding: utf-8 -*-
"""Сводит ручные выгрузки видимого каталога 5ka.ru и прежний checkpoint в Excel.

Скрипт НЕ открывает сайт, НЕ обращается к API каталога и НЕ использует cookies.
Он обрабатывает только JSON/CSV/XLSX, которые пользователь сохранил сам.

Запуск:
    python import_5ka_exports_v7.py
    python import_5ka_exports_v7.py --no-photos

Вход:
    5ka_exports/*.json                 — выгрузки bookmarklet из текущих страниц
    pyaterochka_checkpoint.jsonl       — ранее сохранённые категории v4-v6 (если есть)
    catalog_json/*                     — необязательные разрешённые JSON/CSV/XLSX

Выход:
    pyaterochka_prices_v7.xlsx
    pyaterochka_prices_v7.csv
    photos5ka/PLU.jpg
    pyaterochka_import_report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

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
OUTPUT_XLSX = BASE_DIR / "pyaterochka_prices_v7.xlsx"
OUTPUT_CSV = BASE_DIR / "pyaterochka_prices_v7.csv"
REPORT_JSON = BASE_DIR / "pyaterochka_import_report.json"
PHOTO_DIR = BASE_DIR / "photos5ka"
EXPECTED_SAP_CODE = "35XY"  # подтверждён в успешном тесте; поменяйте при выборе другого магазина

COLUMNS = [
    "SAP магазина",
    "магазин",
    "PLU",
    "название",
    "цена сейчас, ₽",
    "обычная цена, ₽",
    "цена по акции, ₽",
    "скидка, %",
    "маркер акции",
    "в наличии",
    "категории",
    "фото URL",
    "фото файл",
    "карточка товара",
    "страница каталога",
    "время получения",
    "источник",
    "сырой текст карточки",
]

PRICE_RE = re.compile(
    r"(?<!\d)(\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d{2}|[\u00a0\u202f ]\d{2})?)\s*₽",
    re.IGNORECASE,
)
OLD_PRICE_RE = re.compile(
    r"старая\s+цена\s*(\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d{2}|[\u00a0\u202f ]\d{2})?)\s*₽",
    re.IGNORECASE,
)
PROMO_RE = re.compile(r"(?:-\s*\d{1,2}\s*%|%\s*от\s*\d+\s*шт|\b1\s*\+\s*1\b|\bакци\w*\b)", re.IGNORECASE)


def to_price(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # In the observed 5ka API payload prices are strings in RUB, e.g. "4299.0".
        return round(number, 2)
    text = str(value).replace("₽", "").replace("\u00a0", " ").replace("\u202f", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    # Human-readable cards group thousands and kopecks with spaces:
    # "249 99 ₽" or "1 249 99 ₽".
    kopecks_match = re.fullmatch(r"(\d+(?: \d{3})*) (\d{2})", text)
    if kopecks_match:
        left, right = kopecks_match.groups()
        text = f"{left.replace(' ', '')}.{right}"
    else:
        text = text.replace(" ", "")
    text = text.replace(",", ".")
    try:
        return round(float(text), 2)
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
            result = first_image_url(item)
            if result:
                return result
    elif isinstance(value, dict):
        for key in ("normal", "small", "link", "src", "url", "product_card"):
            if key in value:
                result = first_image_url(value[key])
                if result:
                    return result
    return None


def is_product_record(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return (
        (obj.get("plu") is not None or obj.get("id") is not None)
        and bool(obj.get("name") or obj.get("title"))
        and any(k in obj for k in ("prices", "image_links", "image", "media", "uom"))
    )


def walk_products(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        if is_product_record(obj):
            yield obj
            return
        for value in obj.values():
            yield from walk_products(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_products(value)


def parse_visible_card(raw_text: str) -> tuple[float | None, float | None, float | None, str | None, float | None]:
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    all_prices = [to_price(m.group(1)) for m in PRICE_RE.finditer(text)]
    all_prices = [p for p in all_prices if p is not None]
    old_match = OLD_PRICE_RE.search(text)
    old_price = to_price(old_match.group(1)) if old_match else None

    if old_price is not None:
        # The visible card normally renders old price first and current price last.
        current = all_prices[-1] if all_prices else None
        regular = old_price
        discount = current if current is not None and current < regular else None
    elif all_prices:
        current = all_prices[-1]
        regular = None  # ordinary price was not displayed; do not invent it
        discount = None
    else:
        current = regular = discount = None

    promo_match = PROMO_RE.search(text)
    promo_label = promo_match.group(0) if promo_match else None
    percent_match = re.search(r"-\s*(\d{1,2})\s*%", text)
    discount_pct = None
    if regular and current is not None and regular > 0 and current < regular:
        discount_pct = round((regular - current) / regular * 100, 1)
    elif percent_match:
        discount_pct = float(percent_match.group(1))
        if current is not None:
            discount = current  # visible card explicitly marks a percentage discount

    return current, regular, discount, promo_label, discount_pct


def clean_title(title: Any, raw_text: str) -> str | None:
    candidate = str(title or "").strip()
    if candidate:
        return candidate
    text = re.sub(r"\s+", " ", raw_text or "").strip()
    text = re.sub(r"-\s*\d{1,2}\s*%", " ", text)
    text = re.sub(r"%\s*от\s*\d+\s*шт", " ", text, flags=re.I)
    text = re.sub(r"\b1\s*\+\s*1\b", " ", text)
    text = re.sub(r"\b\d[,.]\d{2}\b", " ", text)
    text = re.sub(r"Старая\s+цена.*", " ", text, flags=re.I)
    text = re.sub(r"Цена\s+.*", " ", text, flags=re.I)
    text = re.sub(r"\d+(?:[\u00a0\u202f ]\d{3})*(?:[.,]\d{2}|[\u00a0\u202f ]\d{2})?\s*₽", " ", text)
    text = re.sub(r"\b\d+,\d{2}\b", " ", text)
    text = re.sub(r"В корзину|Нет оценок|Сейчас сезон|Уже сварено", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" -|·")
    return text[:250] or None


def record_from_visible_export(payload: dict[str, Any], product: dict[str, Any]) -> dict[str, Any] | None:
    store = payload.get("store") or {}
    raw_text = str(product.get("raw_text") or "")
    current, regular, discount, promo_label, discount_pct = parse_visible_card(raw_text)
    image_url = first_present(product.get("image_url"), product.get("image"))
    return {
        "SAP магазина": first_present(store.get("sap_code"), payload.get("store_sap_code")),
        "магазин": first_present(store.get("name"), payload.get("store_name")),
        "PLU": first_present(product.get("plu"), product.get("id")),
        "название": clean_title(first_present(product.get("name"), product.get("image_alt")), raw_text),
        "цена сейчас, ₽": current,
        "обычная цена, ₽": regular,
        "цена по акции, ₽": discount,
        "скидка, %": discount_pct,
        "маркер акции": promo_label,
        "в наличии": None,
        "категории": first_present(payload.get("category"), "не указана"),
        "фото URL": image_url,
        "фото файл": None,
        "карточка товара": product.get("product_url"),
        "страница каталога": payload.get("source_url"),
        "время получения": first_present(payload.get("captured_at"), datetime.now(timezone.utc).isoformat()),
        "источник": "5ka.ru — данные видимой страницы (ручной экспорт)",
        "сырой текст карточки": raw_text,
    }


def record_from_api_product(product: dict[str, Any], category: str, sap_code: str | None = None) -> dict[str, Any]:
    prices = product.get("prices") or {}
    regular_raw = first_present(prices.get("regular"), prices.get("priceRegular"), product.get("oldPrice"))
    discount_raw = first_present(prices.get("discount"), product.get("discountPrice"), product.get("salePrice"))
    current_raw = first_present(prices.get("current"), prices.get("price"), product.get("price"), discount_raw, regular_raw)
    media = product.get("media") or {}
    image = first_present(product.get("image_links"), product.get("image"), product.get("image_link"), media.get("image_links"))
    current = to_price(current_raw)
    regular = to_price(regular_raw)
    discount = to_price(discount_raw)
    pct = round((regular - current) / regular * 100, 1) if regular and current is not None and regular > current else None
    return {
        "SAP магазина": sap_code,
        "магазин": None,
        "PLU": first_present(product.get("plu"), product.get("id")),
        "название": first_present(product.get("name"), product.get("title")),
        "цена сейчас, ₽": current,
        "обычная цена, ₽": regular if regular is not None else current,
        "цена по акции, ₽": discount if discount is not None else (current if pct else None),
        "скидка, %": pct,
        "маркер акции": None,
        "в наличии": product.get("is_available"),
        "категории": first_present(product.get("_category"), product.get("category"), category),
        "фото URL": first_image_url(image),
        "фото файл": None,
        "карточка товара": product.get("url") or product.get("product_url"),
        "страница каталога": None,
        "время получения": datetime.now(timezone.utc).isoformat(),
        "источник": "checkpoint/API JSON из предыдущего запуска",
        "сырой текст карточки": None,
    }


def read_json_file(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    records: list[dict[str, Any]] = []
    if isinstance(payload, dict) and payload.get("source") == "5ka.ru-visible-category-links":
        # Category checklist is an aid for manual navigation, not a product data file.
        return []
    if isinstance(payload, dict) and isinstance(payload.get("products"), list):
        store_code = (payload.get("store") or {}).get("sap_code")
        # Current bookmarklet export format.
        if payload.get("source") == "5ka.ru-visible-page-export" or any("raw_text" in p for p in payload["products"] if isinstance(p, dict)):
            for product in payload["products"]:
                if isinstance(product, dict):
                    row = record_from_visible_export(payload, product)
                    if row["PLU"]:
                        records.append(row)
            return records
        category = str(payload.get("category") or path.stem)
        for product in walk_products(payload.get("products")):
            records.append(record_from_api_product(product, category, store_code))
        if records:
            return records
    for product in walk_products(payload):
        records.append(record_from_api_product(product, path.stem))
    return records


def read_checkpoint(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            print(f"  checkpoint: пропущена повреждённая строка {line_no}")
            continue
        category = entry.get("category") or "не указана"
        for product in entry.get("rows") or []:
            row = {column: None for column in COLUMNS}
            row.update({
                "SAP магазина": "35XY",  # последний успешно прочитанный код SAP в тесте пользователя
                "PLU": first_present(product.get("id (plu)"), product.get("plu"), product.get("id")),
                "название": product.get("название") or product.get("name"),
                "цена сейчас, ₽": to_price(product.get("цена, ₽")),
                "обычная цена, ₽": to_price(product.get("обычная цена, ₽")),
                "цена по акции, ₽": to_price(product.get("цена по акции, ₽")),
                "скидка, %": None,
                "в наличии": product.get("в наличии"),
                "категории": first_present(product.get("категория"), category),
                "фото URL": product.get("фото"),
                "фото файл": product.get("фото (файл)"),
                "время получения": None,
                "источник": "checkpoint v4-v6; SAP 35XY из успешного теста",
            })
            if row["PLU"]:
                if row["обычная цена, ₽"] and row["цена сейчас, ₽"] is not None and row["обычная цена, ₽"] > row["цена сейчас, ₽"]:
                    row["цена по акции, ₽"] = row["цена по акции, ₽"] or row["цена сейчас, ₽"]
                    row["скидка, %"] = round((row["обычная цена, ₽"] - row["цена сейчас, ₽"]) / row["обычная цена, ₽"] * 100, 1)
                records.append(row)
    return records


def read_tabular_file(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        try:
            frame = pd.read_csv(path, dtype=str, encoding="utf-8-sig", sep=None, engine="python")
        except UnicodeDecodeError:
            frame = pd.read_csv(path, dtype=str, encoding="cp1251", sep=None, engine="python")
    else:
        frame = pd.read_excel(path, dtype=str)

    aliases = {
        "SAP магазина": {"sap", "sap магазина", "sap code", "store sap"},
        "PLU": {"plu", "id (plu)", "код x5", "код товара x5", "код plu", "код"},
        "название": {"название", "наименование", "товар", "name", "title"},
        "цена сейчас, ₽": {"цена", "цена сейчас", "цена, ₽", "розничная цена", "текущая цена", "price"},
        "обычная цена, ₽": {"обычная цена", "старая цена", "regular price"},
        "цена по акции, ₽": {"акционная цена", "цена по акции", "цена со скидкой", "discount price"},
        "фото URL": {"фото", "изображение", "ссылка на фото", "url фото", "image url"},
        "категории": {"категория", "раздел", "category"},
        "карточка товара": {"url товара", "ссылка на товар", "product url"},
        "в наличии": {"в наличии", "наличие", "доступность", "available", "is_available"},
    }
    reverse = {a.lower().strip(): out for out, names in aliases.items() for a in names}
    rows = []
    for raw in frame.to_dict(orient="records"):
        row = {c: None for c in COLUMNS}
        for key, value in raw.items():
            target = reverse.get(str(key).lower().strip())
            if target and value is not None and str(value).strip():
                row[target] = value
        row["цена сейчас, ₽"] = to_price(row["цена сейчас, ₽"])
        row["обычная цена, ₽"] = to_price(row["обычная цена, ₽"])
        row["цена по акции, ₽"] = to_price(row["цена по акции, ₽"])
        if row["обычная цена, ₽"] and row["цена сейчас, ₽"] and row["обычная цена, ₽"] > row["цена сейчас, ₽"]:
            row["скидка, %"] = round((row["обычная цена, ₽"] - row["цена сейчас, ₽"]) / row["обычная цена, ₽"] * 100, 1)
        if row["PLU"] or row["название"]:
            row["источник"] = f"таблица: {path.name}"
            rows.append(row)
    return rows


def deduplicate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    category_map: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in records:
        plu = str(row.get("PLU") or "").strip()
        store = str(row.get("SAP магазина") or "НЕ УКАЗАН").strip()
        if not plu:
            continue
        key = (store, plu)
        cats = str(row.get("категории") or "").strip()
        if cats:
            category_map[key].update(part.strip() for part in cats.split(" | ") if part.strip())
        previous = grouped.get(key)
        # Prefer the newest timestamp; on equal/missing timestamps, keep the later file row.
        if previous is None or str(row.get("время получения") or "") >= str(previous.get("время получения") or ""):
            grouped[key] = row.copy()

    result = []
    for key, row in grouped.items():
        cats = sorted(category_map[key])
        if cats:
            row["категории"] = " | ".join(cats)
        result.append(row)
    result.sort(key=lambda r: (str(r.get("SAP магазина") or ""), str(r.get("категории") or ""), str(r.get("название") or "")))
    return result


def save_image(url: str | None, plu: Any, delay: float) -> str | None:
    if not url or not str(url).startswith(("https://", "http://")):
        return None
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(plu or "unknown"))
    dest = PHOTO_DIR / f"{safe_id}.jpg"
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


def format_workbook(path: Path, rows: list[dict[str, Any]]) -> None:
    wb = load_workbook(path)
    ws = wb.active
    ws.title = "Товары"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    header_fill = PatternFill("solid", fgColor="166534")
    header_font = Font(bold=True, color="FFFFFF")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 32

    widths = {
        "A": 14, "B": 24, "C": 12, "D": 42, "E": 16, "F": 16,
        "G": 16, "H": 12, "I": 20, "J": 12, "K": 38, "L": 48,
        "M": 28, "N": 48, "O": 48, "P": 24, "Q": 42, "R": 60,
    }
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    price_cols = {5, 6, 7}
    for row_idx in range(2, ws.max_row + 1):
        for col_idx in price_cols:
            ws.cell(row_idx, col_idx).number_format = '#,##0.00 "₽"'
        ws.cell(row_idx, 8).number_format = '0.0"%"'
        for col_idx in (4, 11, 18):
            ws.cell(row_idx, col_idx).alignment = Alignment(vertical="top", wrap_text=True)
        photo_cell = ws.cell(row_idx, 12)
        if isinstance(photo_cell.value, str) and photo_cell.value.startswith(("http://", "https://")):
            photo_cell.hyperlink = photo_cell.value
            photo_cell.font = Font(color="0563C1", underline="single")
        for url_col in (14, 15):
            url_cell = ws.cell(row_idx, url_col)
            if isinstance(url_cell.value, str) and url_cell.value.startswith(("http://", "https://")):
                url_cell.hyperlink = url_cell.value
                url_cell.font = Font(color="0563C1", underline="single")
    if ws.max_row >= 2:
        table = Table(displayName="PyaterochkaProducts", ref=ws.dimensions)
        table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium4", showRowStripes=True, showColumnStripes=False)
        ws.add_table(table)

    # Summary sheet: product-level coverage, grouped by store and category.
    summary = wb.create_sheet("Сводка")
    summary.append(["Показатель", "Значение"])
    metrics = [
        ("Товаров после удаления дублей", len(rows)),
        ("С указанной ценой", sum(r.get("цена сейчас, ₽") is not None for r in rows)),
        ("Со скидкой (цена ниже обычной)", sum(r.get("скидка, %") is not None for r in rows)),
        ("С URL фотографии", sum(bool(r.get("фото URL")) for r in rows)),
        ("Скачано фото", sum(bool(r.get("фото файл")) for r in rows)),
    ]
    for metric in metrics:
        summary.append(list(metric))
    summary.append([])
    summary.append(["SAP магазина", "Категория", "Товаров", "С ценой", "Со скидкой", "Фото URL", "Фото локально"])
    by_cat: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for category in str(row.get("категории") or "не указана").split(" | "):
            by_cat[(str(row.get("SAP магазина") or "НЕ УКАЗАН"), category)].append(row)
    for (sap, cat), items in sorted(by_cat.items()):
        summary.append([
            sap, cat, len(items),
            sum(i.get("цена сейчас, ₽") is not None for i in items),
            sum(i.get("скидка, %") is not None for i in items),
            sum(bool(i.get("фото URL")) for i in items),
            sum(bool(i.get("фото файл")) for i in items),
        ])
    summary.freeze_panes = "A2"
    summary.column_dimensions["A"].width = 24
    summary.column_dimensions["B"].width = 42
    for col in "CDEFG":
        summary.column_dimensions[col].width = 16
    for cell in summary[1]:
        cell.fill = header_fill
        cell.font = header_font
    for row in (8,):
        if row <= summary.max_row:
            for cell in summary[row]:
                cell.fill = header_fill
                cell.font = header_font
    summary.auto_filter.ref = f"A8:G{summary.max_row}" if summary.max_row >= 8 else "A1:B1"
    wb.save(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Импорт ручных выгрузок каталога 5ka.ru в Excel")
    parser.add_argument("--no-photos", action="store_true", help="не скачивать фотографии, оставить URL")
    parser.add_argument("--photo-delay", type=float, default=0.35, help="пауза между скачиваниями фото, секунды (по умолчанию 0.35)")
    args = parser.parse_args()

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    if CHECKPOINT_PATH.exists():
        checkpoint_rows = read_checkpoint(CHECKPOINT_PATH)
        records.extend(checkpoint_rows)
        print(f"Checkpoint v4-v6: загружено товаров — {len(checkpoint_rows)}")

    files = sorted(p for p in EXPORT_DIR.rglob("*") if p.is_file() and p.suffix.lower() == ".json")
    legacy_files = sorted(p for p in LEGACY_DIR.rglob("*") if p.is_file() and p.suffix.lower() in {".json", ".csv", ".xlsx", ".xls"}) if LEGACY_DIR.exists() else []

    for path in files:
        try:
            file_rows = read_json_file(path)
            records.extend(file_rows)
            print(f"5ka_exports/{path.name}: товаров — {len(file_rows)}")
        except Exception as exc:
            errors.append({"файл": str(path), "ошибка": str(exc)})
            print(f"ОШИБКА {path.name}: {exc}")

    for path in legacy_files:
        try:
            file_rows = read_json_file(path) if path.suffix.lower() == ".json" else read_tabular_file(path)
            records.extend(file_rows)
            print(f"catalog_json/{path.name}: товаров — {len(file_rows)}")
        except Exception as exc:
            errors.append({"файл": str(path), "ошибка": str(exc)})
            print(f"ОШИБКА {path.name}: {exc}")

    if not records:
        print("Пока нет данных для Excel.")
        print(f"Откройте 5ka.ru, выберите адрес доставки, перейдите в категорию и сохраните JSON через bookmarklet в папку: {EXPORT_DIR}")
        return 0

    rows = deduplicate(records)
    if not args.no_photos:
        print(f"Скачивание фотографий: {sum(bool(r.get('фото URL')) for r in rows)} URL...")
        for idx, row in enumerate(rows, 1):
            if row.get("фото URL") and not row.get("фото файл"):
                row["фото файл"] = save_image(row["фото URL"], row.get("PLU"), max(args.photo_delay, 0.0))
            if idx % 100 == 0:
                print(f"  фото обработано: {idx}/{len(rows)}")
    else:
        print("Скачивание фото отключено; URL останутся в Excel.")

    frame = pd.DataFrame(rows, columns=COLUMNS)
    frame.to_excel(OUTPUT_XLSX, index=False, sheet_name="Товары")
    frame.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")
    format_workbook(OUTPUT_XLSX, rows)

    actual_sap_codes = sorted({str(r.get("SAP магазина")) for r in rows if r.get("SAP магазина")})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_sap_code": EXPECTED_SAP_CODE,
        "records_before_deduplication": len(records),
        "unique_products": len(rows),
        "store_sap_codes": actual_sap_codes,
        "sap_code_mismatch": bool(actual_sap_codes and any(code != EXPECTED_SAP_CODE for code in actual_sap_codes)),
        "rows_without_sap_code": sum(not r.get("SAP магазина") for r in rows),
        "with_price": sum(r.get("цена сейчас, ₽") is not None for r in rows),
        "with_discount": sum(r.get("скидка, %") is not None for r in rows),
        "with_image_url": sum(bool(r.get("фото URL")) for r in rows),
        "with_local_image": sum(bool(r.get("фото файл")) for r in rows),
        "errors": errors,
        "price_scope": "Цена на сайте/в приложении 5ka.ru по выбранному адресу доставки; не считать гарантированной ценой на полке физического магазина.",
    }
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nГОТОВО")
    print(f"Уникальных товаров: {len(rows)}")
    print(f"С ценой: {report['with_price']}; со скидкой: {report['with_discount']}")
    print(f"С URL фото: {report['with_image_url']}; локально скачано: {report['with_local_image']}")
    print(f"SAP-коды: {', '.join(actual_sap_codes) if actual_sap_codes else 'не указаны'}; ожидаемый — {EXPECTED_SAP_CODE}")
    if report["sap_code_mismatch"] or report["rows_without_sap_code"]:
        print("ВНИМАНИЕ: часть данных не подтверждена для ожидаемого магазина. Проверьте выбор адреса/SAP перед использованием цен.")
    print(f"Excel: {OUTPUT_XLSX}")
    print(f"CSV: {OUTPUT_CSV}")
    print(f"Отчёт: {REPORT_JSON}")
    if errors:
        print(f"Ошибок импорта файлов: {len(errors)} (см. JSON-отчёт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
