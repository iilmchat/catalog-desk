# -*- coding: utf-8 -*-
"""Локальный интерфейс Catalog Desk V16.1.0 (Python stdlib + чистый JS + одиночное и пакетное обогащение).

Запуск: python catalog_ui_server_v14.py
Открывается только на 127.0.0.1, без внешних CDN и npm.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote

import sqlite3
from io import BytesIO

from catalog_config import (
    DEFAULT_CHAIN,
    all_sources,
    allowed_sap,
    chain_from_payload,
    get_store,
    normalize_chain,
    STORES,
    product_url_prefix,
    validate_chain_url,
)
from catalog_migrations import backup_before_v15, migrate_v9_to_v10
from product_data_policy_v17 import (
    PRODUCT_CLASSES, applicability_for, calculate_completeness,
    classify_product, composition_destination,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "ui_v14"
EXPORT_DIR = BASE_DIR / "5ka_exports"
CATEGORY_DIR = BASE_DIR / "5ka_category_lists"
DETAIL_DIR = BASE_DIR / "5ka_product_details"
PHOTO_DIR = BASE_DIR / "photos5ka"
DB_PATH = BASE_DIR / "pyaterochka_catalog_v9.sqlite3"
IMPORTER_PATH = BASE_DIR / "import_5ka_catalog_v9.py"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Обратная совместимость V14: сеть по умолчанию. В V15 заменится на
# список разрешённых SAP через catalog_config (CD-002).
EXPECTED_SAP = get_store(DEFAULT_CHAIN)["default_sap"]

# V16: one-time, short-lived requests for refreshing exactly one product.
_DETAIL_REFRESH_LOCK = threading.Lock()
_DETAIL_REFRESH_REQUESTS: dict[str, dict] = {}
_DETAIL_REFRESH_TTL_SECONDS = 600


def stores_data() -> list[dict]:
    """Список сетей для переключателя в интерфейсе."""
    return [
        {
            "chain": chain, "title": cfg["title"], "domain": cfg["domain"],
            "sap_codes": list(cfg["sap_codes"]), "default_sap": cfg["default_sap"],
            "product_url_prefix": cfg["product_url_prefix"],
        }
        for chain, cfg in STORES.items()
    ]


def compare_data(params: dict) -> dict:
    """Один PLU в разных сетях/магазинах."""
    plu = str((params.get("plu") or [""])[0]).strip()
    if not plu:
        raise ValueError("Не указан PLU")
    conn = connect_db()
    try:
        available = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
        columns = [c for c in ("chain", "sap_code", "plu", "name", "current_price",
                               "regular_price", "discount_pct", "categories",
                               "product_url", "last_seen_at") if c in available]
        rows = conn.execute(
            f"SELECT {','.join(columns)} FROM products WHERE plu=? ORDER BY chain,sap_code",
            (plu,),
        ).fetchall()
        return {"plu": plu, "items": [dict(r) for r in rows]}
    finally:
        conn.close()


def _parse_days(value: str) -> float | None:
    text = str(value or "").lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(дн|ден|день|дней|мес|месяц|год|г\b)", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit.startswith("мес"):
        return amount * 30
    if unit.startswith("год"):
        return amount * 365
    return amount


def search_products(params: dict) -> dict:
    """Поиск по названию, составу, БЖУ, бренду, сроку и цене."""
    chain = request_chain(params)
    raw_query = str((params.get("q") or [""])[0]).strip()
    limit = max(10, min(int((params.get("limit") or ["50"])[0]), 500))
    conditions: dict = {}
    parts = [p.strip() for p in re.split(r",|\s+и\s+|;", raw_query, flags=re.I) if p.strip()]
    for part in parts:
        text = part.lower()
        match = re.search(r"(?:бел(?:ок|ки|ка)|protein)\s*(больше|>)\s*([0-9]+(?:[.,][0-9]+)?)", text)
        if match:
            conditions["min_protein"] = float(match.group(2).replace(",", ".")); continue
        match = re.search(r"жир(?:ов|ы|а)?\s*(меньше|<)\s*([0-9]+(?:[.,][0-9]+)?)", text)
        if match:
            conditions["max_fat"] = float(match.group(1).replace(",", ".")); continue
        match = re.search(r"углевод(?:ов|ы)?\s*(меньше|<)\s*([0-9]+(?:[.,][0-9]+)?)", text)
        if match:
            conditions["max_carbs"] = float(match.group(1).replace(",", ".")); continue
        match = re.search(r"срок[^\d]*([0-9]+)\s*(дн|ден|день|дней|мес|месяц|год)", text)
        if match and ("больше" in text or ">" in text):
            conditions["min_shelf_days"] = _parse_days(match.group(0)); continue
        match = re.search(r"(?:дешевле|до)\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:руб|₽)?", text)
        if match:
            conditions["max_price"] = float(match.group(1).replace(",", ".")); continue
        match = re.search(r"бренд\s+(.+)", text)
        if match:
            conditions["brand"] = match.group(1).strip(); continue
        match = re.search(r"(?:состав\s+)?содержит\s+(.+)", text)
        if match:
            conditions["ingredient"] = match.group(1).strip(); continue
        if "без сахар" in text:
            conditions["sugar_free"] = True

    for key in ("min_protein", "max_fat", "max_carbs", "max_price", "min_shelf_days"):
        if params.get(key):
            conditions[key] = float(str(params[key][0]).replace(",", "."))
    for key in ("brand", "ingredient"):
        if params.get(key):
            conditions[key] = str(params[key][0])
    if params.get("sugar_free"):
        conditions["sugar_free"] = str(params["sugar_free"][0]).lower() in {"1", "yes", "true"}

    conn = connect_db()
    try:
        rows = conn.execute("""
            SELECT p.chain,p.sap_code,p.plu,p.name,p.current_price,p.image_url,p.product_url,
                   p.categories,d.composition,d.nutrition_json,d.attributes_json,d.details_text
            FROM products p
            LEFT JOIN product_details d ON d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu
            WHERE p.chain=?
        """, (chain,)).fetchall()
    finally:
        conn.close()

    items = []
    for row in rows:
        item = dict(row)
        nutrition = item.pop("nutrition_json")
        attrs = item.pop("attributes_json")
        try:
            nutrition = json.loads(nutrition or "{}")
        except Exception:
            nutrition = {}
        try:
            attrs = json.loads(attrs or "{}")
        except Exception:
            attrs = {}
        if not isinstance(nutrition, dict): nutrition = {}
        if not isinstance(attrs, dict): attrs = {}
        composition = str(item.get("composition") or "")
        text = str(item.get("details_text") or "")
        ok = True
        if conditions.get("min_protein") is not None and float(nutrition.get("Белки") or 0) < conditions["min_protein"]:
            ok = False
        if conditions.get("max_fat") is not None and float(nutrition.get("Жиры") or 0) > conditions["max_fat"]:
            ok = False
        if conditions.get("max_carbs") is not None and float(nutrition.get("Углеводы") or 0) > conditions["max_carbs"]:
            ok = False
        if conditions.get("max_price") is not None and (item.get("current_price") or 0) > conditions["max_price"]:
            ok = False
        if conditions.get("brand") and conditions["brand"].lower() not in str(attrs.get("Бренд") or "").lower():
            ok = False
        if conditions.get("ingredient"):
            needle = conditions["ingredient"].lower()
            if needle not in composition.lower() and needle not in text.lower():
                ok = False
        if conditions.get("sugar_free") and re.search(r"сахар|сахароз|глюкоз|фруктоз", composition + " " + text, re.I):
            ok = False
        if conditions.get("min_shelf_days") is not None:
            days = _parse_days(str(attrs.get("Срок хранения") or attrs.get("Срок годности") or ""))
            if days is None or days < conditions["min_shelf_days"]:
                ok = False
        if not ok:
            continue
        item["nutrition"] = nutrition
        item["attributes"] = attrs
        item["quality"] = quality_fields(item)
        items.append(item)
        if len(items) >= limit:
            break
    return {"query": raw_query, "conditions": conditions, "total": len(items), "items": items}


def ingredients_data(params: dict) -> dict:
    """Поиск ингредиента по compositions с пометками-эвристиками."""
    chain = request_chain(params)
    needle = str((params.get("ingredient") or [""])[0]).strip()
    limit = max(10, min(int((params.get("limit") or ["100"])[0]), 500))
    conn = connect_db()
    try:
        rows = conn.execute("""
            SELECT p.sap_code,p.plu,p.name,p.current_price,p.product_url,d.composition
            FROM products p JOIN product_details d
              ON d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu
            WHERE p.chain=? AND d.composition IS NOT NULL AND d.composition!=''
        """, (chain,)).fetchall()
    finally:
        conn.close()
    markers = {
        "пальмовое масло": r"пальмов(?:ое|ый)\s+масло|пальмин",
        "сахар": r"сахар|сахароз|глюкоз|фруктоз",
        "арахис": r"арахис",
        "аллергены": r"аллерген|молок|яйц|орех|глютен|рыб|со[яе]",
    }
    items = []
    for row in rows:
        composition = str(row["composition"] or "")
        if needle and needle.lower() not in composition.lower():
            continue
        found = [name for name, pattern in markers.items() if re.search(pattern, composition, re.I)]
        items.append({
            "sap_code": row["sap_code"], "plu": row["plu"], "name": row["name"],
            "current_price": row["current_price"], "composition": composition,
            "product_url": row["product_url"], "markers": found,
        })
        if len(items) >= limit:
            break
    return {
        "ingredient": needle, "total": len(items), "items": items,
        "disclaimer": "Автоматический анализ текста состава, не лабораторная проверка продукта.",
    }


def compare_table_data(params: dict) -> dict:
    """Сравнение 2–5 товаров с ценой за кг и лучшими значениями."""
    chain = request_chain(params)
    plus = params.get("plu") or []
    if isinstance(plus, str):
        plus = [plus]
    plus = [str(x).strip() for x in plus if str(x).strip()][:5]
    if len(plus) < 2:
        raise ValueError("Для сравнения нужно от 2 до 5 PLU")
    conn = connect_db()
    try:
        marks = ",".join("?" for _ in plus)
        rows = conn.execute(f"""
            SELECT p.sap_code,p.plu,p.name,p.current_price,p.image_url,d.composition,
                   d.nutrition_json,d.attributes_json
            FROM products p LEFT JOIN product_details d
              ON d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu
            WHERE p.chain=? AND p.plu IN ({marks})
        """, (chain, *plus)).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("nutrition_json", "attributes_json"):
            try:
                item[key.replace("_json", "")] = json.loads(item.pop(key) or "{}")
            except Exception:
                item[key.replace("_json", "")] = {}
        weight, _unit = _to_grams(str(item.get("attributes", {}).get("Вес") or ""))
        price = item.get("current_price")
        item["price_per_kg"] = round(price / weight * 1000, 2) if price and weight else None
        items.append(item)
    best = {}
    for key, lower in (("current_price", True), ("price_per_kg", True), ("Белки", False), ("Ккал", True)):
        values = [x.get(key) if key in x else x.get("nutrition", {}).get(key) for x in items]
        values = [float(v) for v in values if v not in (None, "")]
        if values:
            best[key] = min(values) if lower else max(values)
    return {"items": items, "best": best}


def record_operation_error(source: str, message: str, chain: str = DEFAULT_CHAIN) -> None:
    """Записать ошибку операции в журнал."""
    try:
        conn = connect_db()
        try:
            conn.execute(
                "INSERT INTO operation_errors(chain,source,message,created_at) VALUES(?,?,?,?)",
                (normalize_chain(chain), str(source)[:120], str(message)[:800], now_iso()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def errors_data(params: dict) -> list[dict]:
    chain = request_chain(params)
    conn = connect_db()
    try:
        rows = conn.execute("""
            SELECT id,chain,source,message,created_at,resolved,resolved_at
            FROM operation_errors WHERE chain=? ORDER BY created_at DESC LIMIT 200
        """, (chain,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def retry_error(data: dict) -> dict:
    error_id = int(data.get("id") or 0)
    if not error_id:
        raise ValueError("Не указан ID ошибки")
    conn = connect_db()
    try:
        conn.execute(
            "UPDATE operation_errors SET resolved=1,resolved_at=? WHERE id=?", (now_iso(), error_id)
        )
        conn.commit()
        return {"id": error_id, "resolved": True}
    finally:
        conn.close()


def schedule_data() -> dict:
    conn = connect_db()
    try:
        rows = conn.execute("""
            SELECT chain,run_date,status,created_at,finished_at FROM daily_runs
            ORDER BY run_date DESC, chain
        """).fetchall()
        return {
            "chains": list(STORES),
            "runs": [dict(r) for r in rows],
            "execution_mode": "manual_dom_only",
            "automatic_collection": False,
            "note": "Это план ручных выгрузок. Сбор выполняется в открытом браузере из видимого DOM; расписание само не запускает браузерный сбор.",
        }
    finally:
        conn.close()


def set_schedule(data: dict) -> dict:
    chain = request_chain(data)
    run_date = str(data.get("run_date") or now_iso()[:10]).strip()[:10]
    conn = connect_db()
    try:
        conn.execute(
            """INSERT INTO daily_runs(chain,run_date,status,created_at) VALUES(?,?,'planned_manual',?)
               ON CONFLICT(chain,run_date) DO UPDATE SET status=excluded.status""",
            (chain, run_date, now_iso()),
        )
        conn.commit()
        return {"chain": chain, "run_date": run_date, "status": "planned_manual",
                "automatic_collection": False}
    finally:
        conn.close()


def backup_now() -> dict:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Не найдена {DB_PATH.name}")
    dest = DB_PATH.with_name(f"{DB_PATH.stem}_backup_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{DB_PATH.suffix}")
    src = sqlite3.connect(str(DB_PATH), timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    return {"file": dest.name, "created_at": now_iso()}


def api_v1(path: str, params: dict) -> dict:
    """Версионированный локальный API; chain обязателен для товарных роутов."""
    if path == "/api/v1/stores":
        return {"success": True, "data": stores_data()}
    if path == "/api/v1/products":
        data = products_data(params)
        return {"success": True, "data": {"items": data["items"], "total": data["total"],
                                          "page": data["page"], "chain": request_chain(params)}}
    if path == "/api/v1/prices":
        chain = request_chain(params)
        plu = str((params.get("plu") or [""])[0])
        if not plu:
            raise ValueError("Не указан PLU")
        conn = connect_db()
        try:
            rows = conn.execute("""
                SELECT captured_at,imported_at,current_price,regular_price,discount_pct
                FROM price_history WHERE chain=? AND plu=? ORDER BY captured_at,imported_at,id
            """, (chain, plu)).fetchall()
            return {"success": True, "data": {"chain": chain, "plu": plu,
                                              "items": [dict(r) for r in rows]}}
        finally:
            conn.close()
    if path.startswith("/api/v1/products/"):
        parts = path.split("/")
        if len(parts) < 6:
            raise ValueError("Нужно /api/v1/products/{chain}/{plu}")
        params = dict(params)
        params["chain"] = [parts[4]]
        params["plu"] = [parts[5]]
        return {"success": True, "data": product_detail(params)}
    if path == "/api/v1/compare":
        return {"success": True, "data": compare_data(params)}
    if path == "/api/v1/imports":
        return {"success": True, "data": {"runs": schedule_data()["runs"], "job": dict(_JOB)}}
    raise KeyError("Неизвестный роут API v1")


def request_chain(params: dict) -> str:
    """Сеть из query/body; по умолчанию — сеть V14."""
    value = params.get("chain") if isinstance(params, dict) else None
    if isinstance(value, list):
        value = value[0] if value else None
    return normalize_chain(value)


def validate_store(chain: str, sap: str) -> None:
    """Проверить, что SAP относится к сети."""
    if sap not in allowed_sap(chain):
        raise ValueError(
            f"SAP {sap} не относится к сети {chain}; ожидается один из {allowed_sap(chain)}"
        )


def chain_path(chain: str, kind: str) -> Path:
    """Путь папки сети относительно текущего BASE_DIR (уважает подмену в тестах)."""
    return BASE_DIR / get_store(normalize_chain(chain))["paths"][kind]


def store_dirs(kind: str) -> list[Path]:
    """Все папки данного типа по сетям (exports/categories/details/photos)."""
    return [chain_path(chain, kind) for chain in STORES]


_JOB_LOCK = threading.Lock()
_JOB = {
    "running": False, "job_id": 0, "started_at": None, "finished_at": None,
    "return_code": None, "output": "Импорт ещё не запускался.", "error": None, "kind": "idle",
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



def parse_nutrition_from_details_text(text: str) -> dict:
    """Recover nutrition values from the visible 5ka layout where values precede labels."""
    raw = str(text or "")
    start = re.search(r"пищевая\s+ценность", raw, re.I)
    if start:
        block = raw[start.end():start.end()+420]
        stop = re.search(r"\bСостав\b", block, re.I)
        if stop:
            block = block[:stop.start()]
        # Do not mistake the denominator (100 g / 100 ml) for the protein value.
        denominator = re.search(r"(?:на\s*)?100\s*(?:г|мл)\b", block, re.I)
        if denominator:
            block = block[denominator.end():]
    else:
        block = raw[:600]
    result = {}
    specs = {
        "Белки": r"белки",
        "Жиры": r"жиры",
        "Углеводы": r"углеводы",
        "Ккал": r"(?:ккал|калорийность|энергетическая\s+ценность)",
    }
    first_label = re.search(r"(?:белки|жиры|углеводы|ккал)\b", block, re.I)
    first_value = re.search(r"[0-9]+(?:[.,][0-9]+)?", block)
    value_first = bool(first_label and first_value and first_value.start() < first_label.start())
    for label, label_pattern in specs.items():
        before = rf"([0-9]+(?:[.,][0-9]+)?)\s*(?:г|g)?\s*{label_pattern}\b"
        after = rf"{label_pattern}\s*[:：-]?\s*([0-9]+(?:[.,][0-9]+)?)"
        patterns = [before, after] if value_first else [after, before]
        for pattern in patterns:
            match = re.search(pattern, block, re.I)
            if match:
                result[label] = match.group(1).replace(",", ".")
                break
    return result


def recover_composition_from_text(text: str) -> str | None:
    """Восстановить состав из свёрнутого текста карточки (CD-003)."""
    raw = str(text or "")
    match = re.search(
        r"\bСостав\s*([\s\S]{3,900}?)"
        r"(?=\s+(?:Вес|Масса нетто|Объ[её]м|Срок хранения|Срок годности|"
        r"Условия хранения|Бренд|Страна производства|Производитель|"
        r"Тип продукта|Добавки|Острый|Вид упаковки|Назначение соуса)\b|$)",
        raw,
        re.I,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip(" .;:,")
    return value or None


def recover_description_from_text(text: str) -> str | None:
    """Восстановить описание, если закладка не сохранила поле (CD-005)."""
    raw = str(text or "")
    match = re.search(
        r"\bОписание\s*([\s\S]{10,900}?)"
        r"(?=\s+(?:Показать полностью|Состав|Пищевая ценность)\b|$)",
        raw,
        re.I,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip(" .;:,")
    return value or None


def _to_grams(value: str) -> tuple[float | None, str | None]:
    match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(кг|г|мл|л)\b", str(value or ""), re.I)
    if not match:
        return None, None
    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    factor = {"кг": 1000, "г": 1, "мл": 1, "л": 1000}[unit]
    return round(amount * factor, 3), "вес" if unit in ("кг", "г") else "объём"


def weight_contradictions(name: str | None, attributes: dict) -> dict | None:
    """Сравнить массу в названии и в характеристиках."""
    attr_weight = None
    for key in ("Вес", "Масса нетто", "Объем", "Объём"):
        if attributes.get(key):
            attr_weight = _to_grams(str(attributes[key]))
            break
    name_weight = None
    matches = list(re.finditer(r"([0-9]+(?:[.,][0-9]+)?)\s*(кг|г|мл|л)\b", str(name or ""), re.I))
    if matches:
        last = matches[-1]
        name_weight = _to_grams(last.group(0))
    if not attr_weight or not name_weight or attr_weight[1] != name_weight[1]:
        return None
    if not attr_weight[0] or not name_weight[0]:
        return None
    diff = abs(attr_weight[0] - name_weight[0]) / max(attr_weight[0], name_weight[0]) * 100
    if diff <= 5:
        return None
    return {
        "kind": attr_weight[1], "name_value": name_weight[0],
        "attribute_value": attr_weight[0], "difference_percent": round(diff, 1),
    }


def has_complete_nutrition(value) -> bool:
    """True only when all four standard nutrition values are present."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except Exception:
            return False
    if not isinstance(value, dict):
        return False
    expected = ("Белки", "Жиры", "Углеводы", "Ккал")
    return all(
        key in value and value[key] is not None and str(value[key]).strip() != ""
        for key in expected
    )


def quality_fields(row: dict) -> dict:
    """Category-aware completeness; N/A fields are excluded, unknown class is never 100%."""
    text = str(row.get("details_text") or "")
    attrs = row.get("attributes", row.get("attributes_json"))
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs or "{}")
        except Exception:
            attrs = {}
    attrs = attrs if isinstance(attrs, dict) else {}
    nutrition = row.get("nutrition", row.get("nutrition_json"))
    if isinstance(nutrition, str):
        try:
            nutrition = json.loads(nutrition or "{}")
        except Exception:
            nutrition = {}
    nutrition = nutrition if isinstance(nutrition, dict) else {}
    meta = attrs.get("_catalog_meta") if isinstance(attrs.get("_catalog_meta"), dict) else {}
    classification = meta.get("product_class")
    if classification not in PRODUCT_CLASSES:
        classification = classify_product(categories=row.get("categories") or row.get("category") or "")["product_class"]
    applicability = meta.get("field_applicability")
    if not isinstance(applicability, dict):
        applicability = applicability_for(classification)
    composition = row.get("composition") or recover_composition_from_text(text)
    materials = meta.get("materials_text")
    description = row.get("description") or recover_description_from_text(text)
    weight = attrs.get("Вес") or attrs.get("Масса нетто") or attrs.get("Объем") or attrs.get("Объём")
    shelf_life = attrs.get("Срок хранения") or attrs.get("Срок годности")
    policy = calculate_completeness(classification, {
        "name": row.get("name") or row.get("product_name"),
        "price": row.get("current_price"),
        "image": row.get("image_url"),
        "ingredients": composition,
        "nutrition": nutrition if has_complete_nutrition(nutrition) else None,
        "materials": materials,
        "weight": weight,
        "shelf_life": shelf_life,
        "brand": attrs.get("Бренд"),
    }, applicability)
    sources = {}
    if not row.get("composition") and composition:
        sources["состав"] = "анализ текста карточки"
    if not row.get("description") and description:
        sources["описание"] = "анализ текста карточки"
    if not row.get("nutrition_json") and nutrition:
        sources["БЖУ"] = "анализ текста карточки"
    legacy_fields = {
        "название": policy["fields"]["name"]["status"] == "captured",
        "цена": policy["fields"]["price"]["status"] == "captured",
        "фото": policy["fields"]["image"]["status"] == "captured",
        "состав": policy["fields"]["ingredients"]["status"] == "captured",
        "материалы": policy["fields"]["materials"]["status"] == "captured",
        "БЖУ": policy["fields"]["nutrition"]["status"] == "captured",
        "вес": policy["fields"]["weight"]["status"] == "captured",
        "срок хранения": policy["fields"]["shelf_life"]["status"] == "captured",
        "бренд": policy["fields"]["brand"]["status"] == "captured",
    }
    return {
        "percent": policy["percent"], "completed": policy["completed"],
        "required": policy["required"], "product_class": classification,
        "classification_review_required": policy["classification_review_required"],
        "fields": legacy_fields, "field_status": policy["fields"],
        "sources": sources,
        "contradictions": weight_contradictions(
            str(row.get("name") or row.get("product_name") or ""), attrs
        ),
        "disclaimer": "Полнота учитывает только обязательные применимые поля; N/A исключается из знаменателя.",
        "policy_version": policy["policy_version"],
    }


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
            chain TEXT NOT NULL DEFAULT '5ka',
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
            PRIMARY KEY (chain, sap_code, plu)
        );
        CREATE TABLE IF NOT EXISTS product_detail_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL DEFAULT '5ka',
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            file_name TEXT NOT NULL,
            details_json TEXT NOT NULL,
            UNIQUE(source_hash, chain, sap_code, plu)
        );
        CREATE INDEX IF NOT EXISTS idx_product_detail_history_time ON product_detail_history(chain, sap_code, plu, captured_at);
        CREATE TABLE IF NOT EXISTS product_detail_imports (
            source_hash TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            captured_at TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            product_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS favorites (
            chain TEXT NOT NULL DEFAULT '5ka',
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chain, sap_code, plu)
        );
        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL DEFAULT '5ka',
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            image_url TEXT NOT NULL,
            local_file TEXT,
            image_order INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'catalog',
            first_seen_at TEXT,
            last_seen_at TEXT,
            UNIQUE(chain, sap_code, plu, image_url)
        );
        CREATE INDEX IF NOT EXISTS idx_product_images_product ON product_images(chain,sap_code,plu,image_order);
        CREATE TABLE IF NOT EXISTS enrichment_tasks (
            chain TEXT NOT NULL DEFAULT '5ka',
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            last_action_at TEXT NOT NULL,
            opened_at TEXT,
            exported_at TEXT,
            processed_at TEXT,
            skipped_at TEXT,
            note TEXT,
            PRIMARY KEY (chain, sap_code, plu)
        );
        CREATE INDEX IF NOT EXISTS idx_enrichment_tasks_status ON enrichment_tasks(status,last_action_at);
        CREATE TABLE IF NOT EXISTS enrichment_task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL DEFAULT '5ka',
            sap_code TEXT NOT NULL,
            plu TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_enrichment_task_events_product ON enrichment_task_events(chain,sap_code,plu,created_at);
        CREATE TABLE IF NOT EXISTS enrichment_batch_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            active INTEGER NOT NULL DEFAULT 0,
            max_items INTEGER NOT NULL DEFAULT 25,
            delay_seconds INTEGER NOT NULL DEFAULT 15,
            processed_count INTEGER NOT NULL DEFAULT 0,
            category TEXT,
            current_sap TEXT,
            current_plu TEXT,
            current_url TEXT,
            started_at TEXT,
            updated_at TEXT,
            paused_reason TEXT,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS catalog_ui_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operation_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain TEXT NOT NULL DEFAULT '5ka',
            source TEXT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0,
            resolved_at TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_runs (
            chain TEXT NOT NULL,
            run_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            finished_at TEXT,
            PRIMARY KEY(chain, run_date)
        );
        INSERT OR IGNORE INTO enrichment_batch_state(id,active,max_items,delay_seconds,processed_count,updated_at)
        VALUES(1,0,25,15,0,datetime('now'));
        """)
        # V10: добавляем chain и расширяем ключи до засечения очереди.
        migrate_v9_to_v10(conn)
        state_columns = {r[1] for r in conn.execute("PRAGMA table_info(enrichment_batch_state)")}
        if "current_chain" not in state_columns:
            conn.execute("ALTER TABLE enrichment_batch_state ADD COLUMN current_chain TEXT")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS enrichment_batch_states (
            chain TEXT PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 0,
            max_items INTEGER NOT NULL DEFAULT 25,
            delay_seconds INTEGER NOT NULL DEFAULT 15,
            processed_count INTEGER NOT NULL DEFAULT 0,
            category TEXT,
            current_sap TEXT,
            current_plu TEXT,
            current_url TEXT,
            started_at TEXT,
            updated_at TEXT,
            paused_reason TEXT,
            last_error TEXT
        );
        """)
        conn.execute("""
            INSERT OR IGNORE INTO enrichment_batch_states(
                chain,active,max_items,delay_seconds,processed_count,category,
                current_sap,current_plu,current_url,started_at,updated_at,paused_reason,last_error
            )
            SELECT COALESCE(NULLIF(current_chain,''),'5ka'),active,max_items,delay_seconds,
                   processed_count,category,current_sap,current_plu,current_url,
                   started_at,updated_at,paused_reason,last_error
            FROM enrichment_batch_state WHERE id=1
        """)
        # One task per product. Existing task status is never overwritten on startup.
        stamp = now_iso()
        conn.execute("""
            INSERT OR IGNORE INTO enrichment_tasks(
                chain,sap_code,plu,status,created_at,last_action_at,processed_at,note
            )
            SELECT p.chain,p.sap_code,p.plu,
                   CASE WHEN d.sap_code IS NOT NULL AND (
                       COALESCE(d.composition,'')!='' OR COALESCE(d.description,'')!='' OR
                       COALESCE(d.nutrition_json,'{}') NOT IN ('{}','null','') OR
                       COALESCE(d.attributes_json,'{}') NOT IN ('{}','null','') OR
                       COALESCE(d.details_text,'')!=''
                   ) THEN 'processed' ELSE 'pending' END,
                   ?,?,
                   CASE WHEN d.sap_code IS NOT NULL AND (
                       COALESCE(d.composition,'')!='' OR COALESCE(d.description,'')!='' OR
                       COALESCE(d.nutrition_json,'{}') NOT IN ('{}','null','') OR
                       COALESCE(d.attributes_json,'{}') NOT IN ('{}','null','') OR
                       COALESCE(d.details_text,'')!=''
                   ) THEN ? ELSE NULL END,
                   CASE WHEN d.sap_code IS NOT NULL THEN 'Уже есть подробные сведения до V13' ELSE NULL END
            FROM products p LEFT JOIN product_details d
              ON d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu
        """, (stamp, stamp, stamp))
        # Seed the gallery with the main catalogue image and any existing detail image.
        conn.execute("""
            INSERT OR IGNORE INTO product_images(chain,sap_code,plu,image_url,local_file,image_order,source,first_seen_at,last_seen_at)
            SELECT chain,sap_code,plu,image_url,image_file,0,'catalog',first_seen_at,last_seen_at
            FROM products WHERE image_url IS NOT NULL AND image_url!=''
        """)
        conn.execute("""
            INSERT OR IGNORE INTO product_images(chain,sap_code,plu,image_url,image_order,source,first_seen_at,last_seen_at)
            SELECT chain,sap_code,plu,image_url,1,'details',last_captured_at,last_captured_at
            FROM product_details WHERE image_url IS NOT NULL AND image_url!=''
        """)
        # One-time cleanup of legacy V11-V13 recommendation images. Old exporters
        # scanned the whole page; keep only the catalogue main image and the detail
        # export's own primary image. V14 galleries imported after this migration remain.
        gallery_migration = conn.execute(
            "SELECT meta_value FROM catalog_ui_meta WHERE meta_key='gallery_recommendations_cleaned_v14'"
        ).fetchone()
        if not gallery_migration:
            conn.execute("""
                DELETE FROM product_images
                WHERE source='details'
                  AND image_url NOT IN (
                    SELECT p.image_url FROM products p
                    WHERE p.chain=product_images.chain AND p.sap_code=product_images.sap_code AND p.plu=product_images.plu AND p.image_url IS NOT NULL
                    UNION
                    SELECT d.image_url FROM product_details d
                    WHERE d.chain=product_images.chain AND d.sap_code=product_images.sap_code AND d.plu=product_images.plu AND d.image_url IS NOT NULL
                  )
            """)
            conn.execute("INSERT OR REPLACE INTO catalog_ui_meta(meta_key,meta_value) VALUES('gallery_recommendations_cleaned_v14',?)", (now_iso(),))
        # Repair legacy V11-V13 nutrition snapshots. On 5ka some pages render
        # values BEFORE labels (3.5 Белки), while older exports parsed label-first.
        # Repair both the dedicated nutrition JSON and duplicate macro attributes.
        for detail_row in conn.execute(
            "SELECT chain,sap_code,plu,nutrition_json,attributes_json,details_text FROM product_details"
        ).fetchall():
            recovered = parse_nutrition_from_details_text(detail_row[5] or "")
            if len(recovered) >= 2:
                try: current_nutrition = json.loads(detail_row[3] or "{}")
                except Exception: current_nutrition = {}
                try: current_attrs = json.loads(detail_row[4] or "{}")
                except Exception: current_attrs = {}
                if not isinstance(current_nutrition, dict): current_nutrition = {}
                if not isinstance(current_attrs, dict): current_attrs = {}
                current_nutrition.update(recovered)
                for key, value in recovered.items():
                    current_attrs[key] = f"{value}{' ккал' if key == 'Ккал' else ' г'}"
                conn.execute(
                    "UPDATE product_details SET nutrition_json=?,attributes_json=? WHERE chain=? AND sap_code=? AND plu=?",
                    (json.dumps(current_nutrition, ensure_ascii=False),
                     json.dumps(current_attrs, ensure_ascii=False), detail_row[0], detail_row[1], detail_row[2])
                )

        # V15 quality repair: recover composition/description from the saved text.
        if not conn.execute(
            "SELECT 1 FROM catalog_ui_meta WHERE meta_key='quality_repair_v15'"
        ).fetchone():
            for detail_row in conn.execute(
                "SELECT chain,sap_code,plu,composition,description,details_text FROM product_details"
            ).fetchall():
                text = detail_row[5] or ""
                composition = detail_row[3] or recover_composition_from_text(text)
                description = detail_row[4] or recover_description_from_text(text)
                if composition != detail_row[3] or description != detail_row[4]:
                    conn.execute(
                        "UPDATE product_details SET composition=?,description=? "
                        "WHERE chain=? AND sap_code=? AND plu=?",
                        (composition, description, detail_row[0], detail_row[1], detail_row[2]),
                    )
            conn.execute(
                "INSERT OR REPLACE INTO catalog_ui_meta(meta_key,meta_value) VALUES('quality_repair_v15',?)",
                (now_iso(),),
            )

        # Keep detail snapshots consistent with the repaired current card. The raw
        # details_text is preserved; only derived nutrition/attribute fields change.
        for hist_row in conn.execute("SELECT id,details_json FROM product_detail_history").fetchall():
            try: snapshot = json.loads(hist_row[1] or "{}")
            except Exception: continue
            if not isinstance(snapshot, dict): continue
            recovered = parse_nutrition_from_details_text(snapshot.get("details_text") or "")
            if len(recovered) < 2: continue
            nutrition = snapshot.get("nutrition") if isinstance(snapshot.get("nutrition"), dict) else {}
            attrs = snapshot.get("attributes") if isinstance(snapshot.get("attributes"), dict) else {}
            nutrition.update(recovered)
            for key, value in recovered.items():
                attrs[key] = f"{value}{' ккал' if key == 'Ккал' else ' г'}"
            snapshot["nutrition"] = nutrition
            snapshot["attributes"] = attrs
            conn.execute("UPDATE product_detail_history SET details_json=? WHERE id=?",
                         (json.dumps(snapshot, ensure_ascii=False), hist_row[0]))

        # V13 marked any non-empty detail as processed. Re-queue partial cards once
        # so V14 can fill their missing blocks; a successful V14 import marks them processed again.
        incomplete_rows = conn.execute("""
            SELECT p.chain,p.sap_code,p.plu,d.composition,d.description,d.nutrition_json,d.attributes_json,d.details_text
            FROM products p
            LEFT JOIN product_details d ON d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu
            JOIN enrichment_tasks t ON t.chain=p.chain AND t.sap_code=p.sap_code AND t.plu=p.plu
            WHERE t.status='processed'
        """).fetchall()
        for row in incomplete_rows:
            score, _filled = _detail_coverage({
                "composition": row[3], "description": row[4], "nutrition_json": row[5],
                "attributes_json": row[6], "details_text": row[7]
            })
            if score < 100:
                conn.execute("""UPDATE enrichment_tasks SET status='pending',processed_at=NULL,
                    note=? WHERE chain=? AND sap_code=? AND plu=?""",
                    (f"V14: повторное обогащение неполной карточки ({score}%)", row[0], row[1], row[2]))
        conn.commit()
    finally:
        conn.close()


def safe_json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def row_dict(row: sqlite3.Row | None):
    return dict(row) if row is not None else None


def get_price_changes(conn: sqlite3.Connection, limit: int = 500, direction: str = "all",
                      chain: str | None = None) -> list[dict]:
    # Compare consecutive observations for the same chain + SAP + PLU.
    chain_filter = "WHERE h.chain=?" if chain else ""
    sql = f"""
    WITH ordered AS (
      SELECT h.*,
        LAG(current_price) OVER (PARTITION BY chain, sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_current_price,
        LAG(regular_price) OVER (PARTITION BY chain, sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_regular_price,
        LAG(promo_price) OVER (PARTITION BY chain, sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_promo_price,
        LAG(discount_pct) OVER (PARTITION BY chain, sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_discount_pct,
        LAG(promo_label) OVER (PARTITION BY chain, sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_promo_label,
        LAG(captured_at) OVER (PARTITION BY chain, sap_code, plu ORDER BY captured_at, imported_at, id) AS prev_captured_at
      FROM price_history h
      {chain_filter}
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
    params: list = [max(1, min(limit, 2000))]
    if chain:
        params.insert(0, chain)
    rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
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
            "previous_captured_at": r["prev_captured_at"], "chain": r.get("chain"),
            "sap_code": r["sap_code"], "plu": r["plu"],
            "name": r["product_name"], "category": r["category"],
            "old_price": oldp, "new_price": newp, "delta": delta, "delta_pct": pct,
            "old_regular": r["prev_regular_price"], "new_regular": r["regular_price"],
            "old_promo": r["prev_promo_price"], "new_promo": r["promo_price"],
            "old_discount": r["prev_discount_pct"], "new_discount": r["discount_pct"],
            "what_changed": ", ".join(fields), "direction": direction_label,
        })
    return out


def dashboard_data(params: dict | None = None) -> dict:
    chain = request_chain(params or {})
    conn = connect_db()
    try:
        p = conn.execute("""SELECT COUNT(*) products,
            SUM(current_price IS NOT NULL) with_price,
            SUM(discount_pct IS NOT NULL) with_discount,
            SUM(image_url IS NOT NULL AND image_url!='') with_image_url,
            SUM(image_file IS NOT NULL AND image_file!='') with_local_image,
            COUNT(DISTINCT sap_code) sap_count FROM products WHERE chain=?""", (chain,)).fetchone()
        h = conn.execute("SELECT COUNT(*) observations, COUNT(DISTINCT source_hash) observed_exports FROM price_history WHERE chain=?", (chain,)).fetchone()
        ex = conn.execute("SELECT COUNT(*) exports, MAX(imported_at) last_import, MAX(captured_at) last_capture FROM exports WHERE chain=?", (chain,)).fetchone()
        cat = conn.execute("SELECT COUNT(DISTINCT category) FROM product_categories WHERE chain=?", (chain,)).fetchone()[0]
        distinct_pages = conn.execute("SELECT COUNT(DISTINCT sap_code||'|'||COALESCE(category,'')||'|'||COALESCE(page_number,'?')) FROM exports WHERE chain=?", (chain,)).fetchone()[0]
        changes = get_price_changes(conn, 1000, chain=chain)
        recent_exports = [dict(r) for r in conn.execute("""
            SELECT file_name,category,sap_code,page_number,product_rows,unique_products,captured_at,imported_at,source_url
            FROM exports WHERE chain=? ORDER BY imported_at DESC LIMIT 8
        """, (chain,)).fetchall()]
        by_category = category_progress(conn, chain)
        new_since = conn.execute(
            "SELECT COUNT(*) FROM products WHERE chain=? AND first_imported_at >= datetime('now','-7 days')",
            (chain,),
        ).fetchone()[0]
        return {
            "chain": chain, "sap": get_store(chain)["default_sap"],
            "products": int(p["products"] or 0), "with_price": int(p["with_price"] or 0),
            "with_discount": int(p["with_discount"] or 0), "with_image_url": int(p["with_image_url"] or 0),
            "with_local_image": int(p["with_local_image"] or 0), "sap_count": int(p["sap_count"] or 0),
            "observations": int(h["observations"] or 0), "exports": int(ex["exports"] or 0),
            "last_import": ex["last_import"], "last_capture": ex["last_capture"],
            "categories": int(cat or 0), "pages": int(distinct_pages or 0),
            "changes_count": len(changes), "recent_changes": changes[:8],
            "recent_exports": recent_exports, "category_progress": by_category,
            "new_products_7d": int(new_since or 0),
            "with_details": int(conn.execute("SELECT COUNT(*) FROM product_details WHERE chain=?", (chain,)).fetchone()[0]),
            "favorites": int(conn.execute("SELECT COUNT(*) FROM favorites WHERE chain=?", (chain,)).fetchone()[0]),
        }
    finally:
        conn.close()


def category_progress(conn: sqlite3.Connection, chain: str = DEFAULT_CHAIN) -> list[dict]:
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
      LEFT JOIN product_categories p ON p.chain=e.chain AND p.sap_code=e.sap_code AND p.category=e.category
      WHERE e.chain=?
      GROUP BY e.sap_code, e.category
      ORDER BY e.category COLLATE NOCASE
    """, (chain,)).fetchall()
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
    for folder in store_dirs("categories"):
        if not folder.exists():
            continue
        for file in sorted(folder.glob("*.json")):
            try:
                payload = json.loads(file.read_text(encoding="utf-8-sig"))
                for c in payload.get("categories", []):
                    name = str(c.get("name") or "").strip()
                    if not name:
                        continue
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
        chain = request_chain(params)
        where.append("p.chain=?")
        args.append(chain)
        if q:
            like = f"%{q}%"
            where.append("(p.name LIKE ? OR p.plu LIKE ? OR p.product_url LIKE ?)")
            args += [like, like, like]
        if category:
            where.append("EXISTS (SELECT 1 FROM product_categories pc WHERE pc.chain=p.chain AND pc.sap_code=p.sap_code AND pc.plu=p.plu AND pc.category=?)")
            args.append(category)
        if promo == "yes": where.append("p.discount_pct IS NOT NULL")
        elif promo == "no": where.append("p.discount_pct IS NULL")
        if favorites_only:
            where.append("EXISTS (SELECT 1 FROM favorites f WHERE f.chain=p.chain AND f.sap_code=p.sap_code AND f.plu=p.plu)")
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
                 (SELECT COUNT(*) FROM price_history h WHERE h.chain=p.chain AND h.sap_code=p.sap_code AND h.plu=p.plu) AS observations,
                 EXISTS(SELECT 1 FROM favorites f WHERE f.chain=p.chain AND f.sap_code=p.sap_code AND f.plu=p.plu) AS is_favorite,
                 EXISTS(SELECT 1 FROM product_details d WHERE d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu) AS has_details
          FROM products p {clause} ORDER BY {order} LIMIT ? OFFSET ?
        """, args + [limit, (page - 1) * limit]).fetchall()
        items = []
        for r in rows:
            x = dict(r)
            x["chain"] = chain
            x["local_image"] = f"/photos/{Path(str(x['image_file'])).name}" if x.get("image_file") else None
            x["is_favorite"] = bool(x.get("is_favorite"))
            x["has_details"] = bool(x.get("has_details"))
            items.append(x)
        cats = [r[0] for r in conn.execute(
            "SELECT DISTINCT category FROM product_categories WHERE chain=? ORDER BY category COLLATE NOCASE",
            (chain,),
        )]
        return {"items": items, "total": total, "page": page, "limit": limit,
                "pages": (total + limit - 1)//limit, "categories": cats, "favorites_only": favorites_only}
    finally:
        conn.close()


def product_detail(params: dict) -> dict:
    chain = request_chain(params)
    sap = (params.get("sap") or [get_store(chain)["default_sap"]])[0]
    plu = (params.get("plu") or [""])[0]
    if not plu: raise ValueError("Не указан PLU")
    conn = connect_db()
    try:
        r = conn.execute(
            "SELECT * FROM products WHERE chain=? AND sap_code=? AND plu=?", (chain, sap, plu)
        ).fetchone()
        if not r: raise KeyError("Товар не найден")
        product = dict(r)
        product["local_image"] = f"/photos/{Path(str(product['image_file'])).name}" if product.get("image_file") else None
        product["is_favorite"] = bool(conn.execute(
            "SELECT 1 FROM favorites WHERE chain=? AND sap_code=? AND plu=?", (chain, sap, plu)
        ).fetchone())
        history = [dict(x) for x in conn.execute("""
            SELECT id,captured_at,imported_at,current_price,regular_price,promo_price,discount_pct,promo_label,
                   category,product_name,image_url,file_name,source_url
            FROM price_history WHERE chain=? AND sap_code=? AND plu=? ORDER BY captured_at,imported_at,id
        """, (chain, sap, plu)).fetchall()]
        d = conn.execute(
            "SELECT * FROM product_details WHERE chain=? AND sap_code=? AND plu=?", (chain, sap, plu)
        ).fetchone()
        extended = dict(d) if d else None
        if extended:
            for key in ("nutrition_json", "attributes_json"):
                try: extended[key.removesuffix("_json")] = json.loads(extended.get(key) or "{}")
                except Exception: extended[key.removesuffix("_json")] = {}
            extended["catalog_metadata"] = (extended.get("attributes") or {}).get("_catalog_meta", {})
        detail_history = []
        if d:
            for row in conn.execute("""
                SELECT captured_at, imported_at, file_name, details_json
                FROM product_detail_history WHERE chain=? AND sap_code=? AND plu=? ORDER BY captured_at DESC, imported_at DESC LIMIT 20
            """, (chain, sap, plu)).fetchall():
                item = dict(row)
                try: item["details"] = json.loads(item.pop("details_json"))
                except Exception: item["details"] = {}
                detail_history.append(item)
        images = []
        for image_row in conn.execute("""
            SELECT image_url,local_file,image_order,source FROM product_images
            WHERE chain=? AND sap_code=? AND plu=? ORDER BY image_order,id
        """, (chain, sap, plu)).fetchall():
            image_item = dict(image_row)
            local = image_item.get("local_file")
            image_item["local_url"] = f"/photos/{Path(str(local)).name}" if local else None
            images.append(image_item)
        if not images and product.get("image_url"):
            images = [{"image_url":product.get("image_url"),"local_file":product.get("image_file"),
                       "local_url":product.get("local_image"),"image_order":0,"source":"catalog"}]
        quality_input = {**product, **(extended or {})}
        return {"product": product, "history": history, "details": extended,
                "detail_history": detail_history, "images": images,
                "quality": quality_fields(quality_input)}
    finally:
        conn.close()


def pending_exports_data() -> list[dict]:
    """Pending catalogue and product-detail JSON files, deduplicated by SHA-256."""
    import hashlib
    pending = []
    for folder in store_dirs("exports"):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.json")):
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
                                    "products": len(payload["products"]), "captured_at": payload.get("captured_at"),
                                    "chain": chain_from_payload(payload), "kind": "products"})
            except Exception:
                continue
    for folder in store_dirs("details"):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*.json")):
            try:
                raw = path.read_bytes(); payload = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(payload, dict) or payload.get("source") not in all_sources():
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                conn = connect_db()
                try: found = conn.execute("SELECT 1 FROM product_detail_imports WHERE source_hash=?", (digest,)).fetchone()
                finally: conn.close()
                if not found:
                    prod = payload.get("product") or {}
                    pending.append({"name":path.name,"category":"Доп. сведения товара",
                                    "products":1 if prod.get("plu") else 0,"captured_at":payload.get("captured_at"),
                                    "chain": chain_from_payload(payload), "kind":"details"})
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
        for folder in store_dirs("exports"):
            for path in folder.glob("*.json"):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                    pagination = payload.get("pagination") or {}
                    max_page = pagination.get("max_page_linked")
                    visible = pagination.get("visible_page_numbers") or []
                    if max_page is None and not visible:
                        continue
                    import hashlib
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    conn.execute(
                        "UPDATE exports SET max_page_linked=?, visible_page_numbers=? WHERE source_hash=?",
                        (int(max_page) if max_page is not None else None, safe_json(visible), digest),
                    )
                except Exception:
                    continue
        conn.commit()
    finally:
        conn.close()


def _prepare_product_semantics(prod: dict, nutrition: dict, attrs: dict, details_text: str) -> tuple[str | None, str | None, dict, dict]:
    """Normalize generic retailer composition into ingredients or materials and retain provenance."""
    attrs = dict(attrs or {})
    supplied_meta = prod.get("catalog_metadata") if isinstance(prod.get("catalog_metadata"), dict) else {}
    classification = prod.get("product_class") or supplied_meta.get("product_class")
    taxonomy = prod.get("category_path") or prod.get("categories") or supplied_meta.get("category_path") or ""
    inferred = classify_product(categories=taxonomy)
    if classification not in PRODUCT_CLASSES:
        classification = inferred["product_class"]
    basis = supplied_meta.get("classification_basis") or prod.get("classification_basis") or inferred["classification_basis"]
    confidence = supplied_meta.get("classification_confidence", prod.get("classification_confidence", inferred["classification_confidence"]))
    applicability = prod.get("field_applicability") or supplied_meta.get("field_applicability")
    if not isinstance(applicability, dict):
        applicability = applicability_for(classification)
    source_composition = prod.get("composition") or recover_composition_from_text(details_text)
    destination = composition_destination(classification, prod.get("composition_source_label") or "Состав")
    ingredients = prod.get("ingredients") or (source_composition if destination == "ingredients" else None)
    materials = prod.get("materials") or supplied_meta.get("materials_text") or (source_composition if destination == "materials" else None)
    unclassified = source_composition if destination == "unclassified" else None
    meta = {
        "schema_version": 1, "policy_version": "1",
        "product_class": classification, "classification_basis": basis,
        "classification_confidence": confidence, "category_path": taxonomy,
        "field_applicability": applicability,
        "materials_text": materials, "unclassified_composition_text": unclassified,
        "composition_kind": destination,
        "extraction_status": {
            "name": "captured" if prod.get("name") else "not_found",
            "price": "captured" if prod.get("current_price") is not None else "not_found",
            "image": "captured" if prod.get("image_url") else "not_found",
            "ingredients": "captured" if ingredients else ("not_applicable" if applicability.get("ingredients") == "not_applicable" else "not_found"),
            "materials": "captured" if materials else ("not_applicable" if applicability.get("materials") == "not_applicable" else "not_found"),
            "nutrition": "captured" if has_complete_nutrition(nutrition) else ("not_applicable" if applicability.get("nutrition") == "not_applicable" else "not_found"),
            "weight": "captured" if any(attrs.get(k) for k in ("Вес", "Масса нетто", "Объем", "Объём")) else "not_found",
            "shelf_life": "captured" if attrs.get("Срок хранения") or attrs.get("Срок годности") else ("not_applicable" if applicability.get("shelf_life") == "not_applicable" else "not_found"),
            "brand": "captured" if attrs.get("Бренд") else "not_found",
            "description": "captured" if prod.get("description") else "not_found",
        },
        "provenance": {"source_url": prod.get("product_url"), "captured_at": prod.get("captured_at") or None},
    }
    attrs["_catalog_meta"] = meta
    return ingredients, materials, attrs, meta


def normalize_details_payload(payload: dict) -> list[dict]:
    products = payload.get("products")
    if isinstance(products, list):
        return [x for x in products if isinstance(x, dict)]
    product = payload.get("product")
    return [product] if isinstance(product, dict) else []


def _image_alt_matches_product(product_name: str, alt: str) -> bool:
    """Return true only when image alt text plausibly identifies this product."""
    import re as _re
    stop = {"для", "из", "и", "с", "со", "на", "в", "по", "от", "к", "г", "кг", "мл", "шт"}
    tokens = lambda value: {x for x in _re.sub(r"[^\w]+", " ", str(value or "").lower(), flags=_re.UNICODE).split()
                            if len(x) > 2 and x not in stop}
    a, b = tokens(product_name), tokens(alt)
    if not a or not b:
        return False
    return len(a & b) / max(1, min(len(a), len(b))) >= 0.34


def product_gallery_urls(prod: dict) -> list[str]:
    """Keep primary image plus gallery images whose alt text matches the product.

    Old exporters scanned the full page and sometimes picked recommendation cards.
    A URL without matching alt is not accepted as an additional gallery image.
    """
    name = str(prod.get("name") or "")
    urls: list[str] = []
    main_url = str(prod.get("image_url") or "").strip()
    main_alt = str(prod.get("image_alt") or "").strip()
    if main_url.startswith("https://") and (not main_alt or _image_alt_matches_product(name, main_alt)):
        urls.append(main_url)
    gallery = prod.get("image_gallery") if isinstance(prod.get("image_gallery"), list) else []
    for image_item in gallery:
        if not isinstance(image_item, dict):
            continue
        url = str(image_item.get("url") or "").strip()
        alt = str(image_item.get("alt") or "").strip()
        if not url.startswith("https://") or not alt:
            continue
        if not _image_alt_matches_product(name, alt):
            continue
        if url not in urls:
            urls.append(url)
        if len(urls) >= 12:
            break
    return urls[:12]



def create_detail_refresh_request(data: dict) -> dict:
    """Create a short-lived one-time request bound to one exact product."""
    chain = normalize_chain(data.get("chain"))
    sap = str(data.get("sap_code") or data.get("sap") or "").strip()
    plu = str(data.get("plu") or "").strip()
    if not sap:
        raise ValueError("Не указан SAP")
    if not plu or not plu.isdigit():
        raise ValueError("PLU должен содержать только цифры")
    validate_store(chain, sap)
    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT product_url FROM products WHERE chain=? AND sap_code=? AND plu=?",
            (chain, sap, plu),
        ).fetchone()
        if not row:
            raise ValueError("Товар с указанными сетью, SAP и PLU не найден")
        product_url = str(row["product_url"] or "").strip()
        if not product_url:
            raise ValueError("У товара нет ссылки на карточку магазина")
        validate_chain_url(chain, product_url)
    finally:
        conn.close()
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _DETAIL_REFRESH_LOCK:
        expired = [key for key, value in _DETAIL_REFRESH_REQUESTS.items()
                   if value["expires_at"] <= now]
        for key in expired:
            _DETAIL_REFRESH_REQUESTS.pop(key, None)
        if len(_DETAIL_REFRESH_REQUESTS) >= 500:
            raise ValueError("Слишком много активных запросов обновления; повторите позже")
        _DETAIL_REFRESH_REQUESTS[token] = {
            "chain": chain, "sap_code": sap, "plu": plu,
            "product_url": product_url, "expires_at": now + _DETAIL_REFRESH_TTL_SECONDS,
            "recording": False,
        }
    return {
        "token": token, "chain": chain, "sap_code": sap, "plu": plu,
        "product_url": product_url, "expires_in": _DETAIL_REFRESH_TTL_SECONDS,
    }


def claim_detail_refresh(params: dict) -> dict:
    """Read the product identity bound to a one-time refresh token."""
    token = str((params.get("token") or [""])[0]).strip()
    if not token:
        raise ValueError("Не указан токен одиночного обновления")
    with _DETAIL_REFRESH_LOCK:
        request = _DETAIL_REFRESH_REQUESTS.get(token)
        if not request or request["expires_at"] <= time.monotonic():
            _DETAIL_REFRESH_REQUESTS.pop(token, None)
            raise ValueError("Запрос обновления истёк или уже использован")
        if request["recording"]:
            raise ValueError("Этот запрос уже обрабатывается")
        return {
            "chain": request["chain"], "sap_code": request["sap_code"],
            "plu": request["plu"], "product_url": request["product_url"],
        }


def import_single_detail_snapshot(payload: dict, expected: dict) -> dict:
    """Import one validated snapshot without touching enrichment queue or batch state."""
    if not isinstance(payload, dict) or payload.get("source") not in all_sources():
        raise ValueError("Неверный формат подробной карточки")
    chain = chain_from_payload(payload)
    if chain != expected["chain"]:
        raise ValueError(f"Ожидалась сеть {expected['chain']}, получена {chain}")
    validate_chain_url(chain, payload.get("source_url"))
    store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
    sap = str(store.get("sap_code") or payload.get("sap_code") or "").strip()
    if sap != expected["sap_code"]:
        raise ValueError("SAP карточки не совпадает с одноразовым запросом")
    validate_store(chain, sap)
    products = normalize_details_payload(payload)
    if len(products) != 1:
        raise ValueError("Одиночное обновление должно содержать ровно один товар")
    prod = products[0]
    plu = str(prod.get("plu") or "").strip()
    if plu != expected["plu"] or not plu.isdigit():
        raise ValueError("PLU карточки не совпадает с одноразовым запросом")
    product_url = str(prod.get("product_url") or "").strip()
    if not product_url:
        raise ValueError("В карточке отсутствует product_url")
    validate_chain_url(chain, product_url)

    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    captured = str(payload.get("captured_at") or now_iso())
    imported = now_iso()
    detail_dir = chain_path(chain, "details") / "_single_refresh"
    detail_dir.mkdir(parents=True, exist_ok=True)
    safe_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    file_name = f"single_refresh_{chain}_{sap}_{plu}_{safe_stamp}_{uuid.uuid4().hex[:8]}.json"
    path = detail_dir / file_name
    path.write_bytes(raw)

    conn = connect_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM products WHERE chain=? AND sap_code=? AND plu=?",
            (chain, sap, plu),
        ).fetchone()
        if not exists:
            raise ValueError("Товар больше не существует в каталоге")
        if conn.execute(
            "SELECT 1 FROM product_detail_imports WHERE source_hash=?", (digest,)
        ).fetchone():
            raise ValueError("Этот снимок уже импортирован")

        nutrition = prod.get("nutrition") if isinstance(prod.get("nutrition"), dict) else {}
        attrs = prod.get("attributes") if isinstance(prod.get("attributes"), dict) else {}
        recovered_nutrition = parse_nutrition_from_details_text(
            prod.get("details_text") or prod.get("raw_text") or ""
        )
        if len(recovered_nutrition) >= 2:
            nutrition = {**nutrition, **recovered_nutrition}
            attrs = dict(attrs)
            for nutrient, value in recovered_nutrition.items():
                attrs[nutrient] = f"{value}{' ккал' if nutrient == 'Ккал' else ' г'}"
        text_for_repair = prod.get("details_text") or prod.get("raw_text") or ""
        composition, materials, attrs, catalog_meta = _prepare_product_semantics(
            prod, nutrition, attrs, text_for_repair
        )
        description = prod.get("description") or recover_description_from_text(text_for_repair)
        detail_snapshot = {
            "name": prod.get("name"), "product_url": product_url,
            "image_url": prod.get("image_url"), "description": description,
            "composition": composition, "materials": materials,
            "catalog_metadata": catalog_meta, "nutrition": nutrition,
            "attributes": attrs, "details_text": text_for_repair,
        }
        details_json = safe_json(detail_snapshot)
        old = conn.execute(
            "SELECT first_imported_at FROM product_details WHERE chain=? AND sap_code=? AND plu=?",
            (chain, sap, plu),
        ).fetchone()
        first_imported = old[0] if old else imported
        conn.execute("""
            INSERT INTO product_detail_history(
                chain,sap_code,plu,captured_at,imported_at,source_hash,file_name,details_json
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source_hash,chain,sap_code,plu) DO NOTHING
        """, (chain,sap,plu,captured,imported,digest,file_name,details_json))
        conn.execute("""
            INSERT INTO product_details(
                chain,sap_code,plu,product_name,product_url,image_url,description,composition,
                nutrition_json,attributes_json,details_text,first_imported_at,last_imported_at,
                last_captured_at,last_source_hash
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chain,sap_code,plu) DO UPDATE SET
                product_name=COALESCE(excluded.product_name,product_details.product_name),
                product_url=COALESCE(excluded.product_url,product_details.product_url),
                image_url=COALESCE(excluded.image_url,product_details.image_url),
                description=COALESCE(excluded.description,product_details.description),
                composition=CASE
                    WHEN instr(excluded.attributes_json, '"_catalog_meta"')>0 THEN excluded.composition
                    ELSE COALESCE(excluded.composition,product_details.composition) END,
                nutrition_json=CASE WHEN excluded.nutrition_json!='{}' THEN excluded.nutrition_json ELSE product_details.nutrition_json END,
                attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE product_details.attributes_json END,
                details_text=COALESCE(excluded.details_text,product_details.details_text),
                last_imported_at=excluded.last_imported_at,
                last_captured_at=excluded.last_captured_at,last_source_hash=excluded.last_source_hash
        """, (chain,sap,plu,prod.get("name"),product_url,prod.get("image_url"),description,
              composition,safe_json(nutrition),safe_json(attrs),
              prod.get("details_text") or prod.get("raw_text"),
              first_imported,imported,captured,digest))
        for image_idx, image_url in enumerate(product_gallery_urls(prod)[:12]):
            conn.execute("""
                INSERT INTO product_images(
                    chain,sap_code,plu,image_url,image_order,source,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(chain,sap_code,plu,image_url) DO UPDATE SET
                    image_order=MIN(product_images.image_order,excluded.image_order),
                    last_seen_at=excluded.last_seen_at
            """, (chain,sap,plu,image_url,image_idx,"details",captured,captured))
        conn.execute("""
            INSERT INTO product_detail_imports(source_hash,file_name,captured_at,imported_at,product_count)
            VALUES(?,?,?,?,1)
        """, (digest,file_name,captured,imported))
        conn.commit()
        return {
            "accepted": True, "chain": chain, "sap_code": sap, "plu": plu,
            "file_name": file_name, "captured_at": captured, "imported_at": imported,
            "queue_changed": False,
        }
    except Exception:
        conn.rollback()
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    finally:
        conn.close()


def record_single_detail_refresh(data: dict) -> dict:
    """Consume a token only after its exact one-product snapshot imports successfully."""
    token = str(data.get("token") or "").strip()
    if not token:
        raise ValueError("Не указан токен одиночного обновления")
    with _DETAIL_REFRESH_LOCK:
        request = _DETAIL_REFRESH_REQUESTS.get(token)
        if not request or request["expires_at"] <= time.monotonic():
            _DETAIL_REFRESH_REQUESTS.pop(token, None)
            raise ValueError("Запрос обновления истёк или уже использован")
        if request["recording"]:
            raise ValueError("Этот запрос уже обрабатывается")
        request["recording"] = True
        expected = dict(request)
    try:
        result = import_single_detail_snapshot(data.get("payload") or {}, expected)
    except Exception:
        with _DETAIL_REFRESH_LOCK:
            current = _DETAIL_REFRESH_REQUESTS.get(token)
            if current:
                current["recording"] = False
        raise
    with _DETAIL_REFRESH_LOCK:
        _DETAIL_REFRESH_REQUESTS.pop(token, None)
    return result


def import_pending_details() -> tuple[int, int, int, list[str]]:
    """Import manual product-page detail snapshots into SQLite, preserving every import."""
    import hashlib
    detail_dirs = [p for p in store_dirs("details") if p.exists()]
    if not detail_dirs: return 0, 0, 0, []
    imported_files = imported_products = skipped = 0
    errors: list[str] = []
    conn = connect_db()
    try:
        for path in sorted(p for folder in detail_dirs for p in folder.glob("*.json")):
            try:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if conn.execute("SELECT 1 FROM product_detail_imports WHERE source_hash=?", (digest,)).fetchone():
                    skipped += 1; continue
                payload = json.loads(raw.decode("utf-8-sig"))
                if payload.get("source") not in all_sources():
                    continue
                chain = chain_from_payload(payload)
                validate_chain_url(chain, payload.get("source_url"))
                store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
                sap = str(store.get("sap_code") or payload.get("sap_code") or get_store(chain)["default_sap"]).strip()
                try:
                    validate_store(chain, sap)
                except ValueError as exc:
                    errors.append(f"{path.name}: {exc}"); continue
                captured = str(payload.get("captured_at") or now_iso())
                imported = now_iso()
                products = normalize_details_payload(payload)
                if not products:
                    errors.append(f"{path.name}: нет блока product/products"); continue
                conn.execute("BEGIN")
                accepted = 0
                for prod in products:
                    validate_chain_url(chain, prod.get("product_url"))
                    plu = str(prod.get("plu") or "").strip()
                    if not plu: continue
                    nutrition = prod.get("nutrition") if isinstance(prod.get("nutrition"), dict) else {}
                    attrs = prod.get("attributes") if isinstance(prod.get("attributes"), dict) else {}
                    recovered_nutrition = parse_nutrition_from_details_text(prod.get("details_text") or prod.get("raw_text") or "")
                    if len(recovered_nutrition) >= 2:
                        nutrition = {**nutrition, **recovered_nutrition}
                        attrs = dict(attrs)
                        for nutrient, value in recovered_nutrition.items():
                            attrs[nutrient] = f"{value}{' ккал' if nutrient == 'Ккал' else ' г'}"
                    text_for_repair = prod.get("details_text") or prod.get("raw_text") or ""
                    composition, materials, attrs, catalog_meta = _prepare_product_semantics(
                        prod, nutrition, attrs, text_for_repair
                    )
                    description = prod.get("description") or recover_description_from_text(text_for_repair)
                    detail_snapshot = {
                        "name": prod.get("name"), "product_url": prod.get("product_url"),
                        "image_url": prod.get("image_url"), "description": description,
                        "composition": composition, "materials": materials,
                        "catalog_metadata": catalog_meta, "nutrition": nutrition,
                        "attributes": attrs, "details_text": text_for_repair,
                    }
                    details_json = safe_json(detail_snapshot)
                    old = conn.execute(
                        "SELECT first_imported_at FROM product_details WHERE chain=? AND sap_code=? AND plu=?",
                        (chain, sap, plu),
                    ).fetchone()
                    first_imported = old[0] if old else imported
                    conn.execute("""
                        INSERT INTO product_detail_history(chain,sap_code,plu,captured_at,imported_at,source_hash,file_name,details_json)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_hash,chain,sap_code,plu) DO NOTHING
                    """, (chain,sap,plu,captured,imported,digest,path.name,details_json))
                    conn.execute("""
                        INSERT INTO product_details(chain,sap_code,plu,product_name,product_url,image_url,description,composition,
                            nutrition_json,attributes_json,details_text,first_imported_at,last_imported_at,last_captured_at,last_source_hash)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(chain,sap_code,plu) DO UPDATE SET
                            product_name=COALESCE(excluded.product_name,product_details.product_name),
                            product_url=COALESCE(excluded.product_url,product_details.product_url),
                            image_url=COALESCE(excluded.image_url,product_details.image_url),
                            description=COALESCE(excluded.description,product_details.description),
                            composition=CASE
                    WHEN instr(excluded.attributes_json, '"_catalog_meta"')>0 THEN excluded.composition
                    ELSE COALESCE(excluded.composition,product_details.composition) END,
                            nutrition_json=CASE WHEN excluded.nutrition_json!='{}' THEN excluded.nutrition_json ELSE product_details.nutrition_json END,
                            attributes_json=CASE WHEN excluded.attributes_json!='{}' THEN excluded.attributes_json ELSE product_details.attributes_json END,
                            details_text=COALESCE(excluded.details_text,product_details.details_text),
                            last_imported_at=excluded.last_imported_at,last_captured_at=excluded.last_captured_at,last_source_hash=excluded.last_source_hash
                    """, (chain,sap,plu,prod.get("name"),prod.get("product_url"),prod.get("image_url"),description,
                          composition,safe_json(nutrition),safe_json(attrs),prod.get("details_text") or prod.get("raw_text"),
                          first_imported,imported,captured,digest))
                    gallery_urls = product_gallery_urls(prod)
                    for image_idx, image_url in enumerate(gallery_urls[:12]):
                        conn.execute("""
                            INSERT INTO product_images(chain,sap_code,plu,image_url,image_order,source,first_seen_at,last_seen_at)
                            VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(chain,sap_code,plu,image_url) DO UPDATE SET
                                image_order=MIN(product_images.image_order,excluded.image_order),
                                last_seen_at=excluded.last_seen_at
                        """, (chain,sap,plu,image_url,image_idx,"details",captured,captured))
                    # A successfully imported detail JSON marks this product as processed.
                    task_stamp = now_iso()
                    conn.execute("""
                        INSERT OR IGNORE INTO enrichment_tasks(chain,sap_code,plu,status,created_at,last_action_at)
                        VALUES(?,?,?,'pending',?,?)
                    """, (chain, sap, plu, task_stamp, task_stamp))
                    conn.execute("""
                        UPDATE enrichment_tasks SET status='processed',last_action_at=?,processed_at=?,note=?
                        WHERE chain=? AND sap_code=? AND plu=?
                    """, (task_stamp, task_stamp, f"Импортировано из {path.name}", chain, sap, plu))
                    conn.execute("INSERT INTO enrichment_task_events(chain,sap_code,plu,action,created_at,note) VALUES(?,?,?,?,?,?)",
                                 (chain, sap, plu, "processed", task_stamp, path.name))
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
    for message in errors:
        record_operation_error("details_import", message)
    return imported_files, imported_products, skipped, errors


def analytics_data(params: dict | None = None) -> dict:
    chain = request_chain(params or {})
    conn = connect_db()
    try:
        daily = [dict(r) for r in conn.execute("""
            SELECT substr(captured_at,1,10) AS day, ROUND(AVG(current_price),2) AS avg_price,
                   COUNT(*) AS observations,
                   ROUND(AVG(CASE WHEN discount_pct IS NOT NULL THEN discount_pct END),1) AS avg_discount
            FROM price_history WHERE chain=? AND current_price IS NOT NULL
            GROUP BY substr(captured_at,1,10) ORDER BY day DESC LIMIT 30
        """, (chain,)).fetchall()][::-1]
        top_discounts = [dict(r) for r in conn.execute("""
            SELECT chain,sap_code,plu,name,current_price,regular_price,discount_pct,categories,image_file,image_url
            FROM products WHERE chain=? AND discount_pct IS NOT NULL ORDER BY discount_pct DESC,current_price ASC LIMIT 12
        """, (chain,)).fetchall()]
        changes = get_price_changes(conn, 2000, "all", chain=chain)
        ups = sorted([x for x in changes if x.get("delta") is not None and x["delta"]>0], key=lambda x:(x.get("delta_pct") or 0), reverse=True)[:10]
        downs = sorted([x for x in changes if x.get("delta") is not None and x["delta"]<0], key=lambda x:(x.get("delta_pct") or 0))[:10]
        summary = conn.execute("""
            SELECT COUNT(*) AS products, ROUND(AVG(current_price),2) AS avg_price,
                   ROUND(AVG(discount_pct),1) AS avg_discount, SUM(discount_pct IS NOT NULL) AS discounted
            FROM products WHERE chain=?
        """, (chain,)).fetchone()
        return {"chain":chain,"daily":daily,"top_discounts":top_discounts,"biggest_up":ups,
                "biggest_down":downs,"summary":dict(summary)}
    finally: conn.close()


def favorites_data(params: dict | None = None) -> list[dict]:
    chain = request_chain(params or {})
    conn = connect_db()
    try:
        rows = conn.execute("""
            SELECT p.chain,p.sap_code,p.plu,p.name,p.current_price,p.regular_price,p.discount_pct,p.categories,
                   p.image_url,p.image_file,p.last_seen_at,f.created_at
            FROM favorites f JOIN products p
              ON p.chain=f.chain AND p.sap_code=f.sap_code AND p.plu=f.plu
            WHERE f.chain=?
            ORDER BY f.created_at DESC
        """, (chain,)).fetchall()
        items=[]
        for r in rows:
            x=dict(r); x["local_image"] = f"/photos/{Path(str(x['image_file'])).name}" if x.get("image_file") else None
            items.append(x)
        return items
    finally: conn.close()


def set_favorite(data: dict) -> dict:
    chain = request_chain(data)
    sap = str(data.get("sap_code") or get_store(chain)["default_sap"]).strip()
    plu = str(data.get("plu") or "").strip()
    enabled = bool(data.get("favorite"))
    if not plu: raise ValueError("Не указан PLU")
    validate_store(chain, sap)
    conn=connect_db()
    try:
        exists=conn.execute(
            "SELECT 1 FROM products WHERE chain=? AND sap_code=? AND plu=?", (chain, sap, plu)
        ).fetchone()
        if not exists: raise KeyError("Товар не найден")
        if enabled:
            conn.execute("INSERT OR IGNORE INTO favorites(chain,sap_code,plu,created_at) VALUES(?,?,?,?)",(chain,sap,plu,now_iso()))
        else:
            conn.execute("DELETE FROM favorites WHERE chain=? AND sap_code=? AND plu=?",(chain,sap,plu))
        conn.commit()
        return {"chain":chain,"sap_code":sap,"plu":plu,"favorite":enabled}
    finally: conn.close()


def _detail_coverage(row: dict) -> tuple[int, list[str]]:
    """Queue coverage mirrors category-aware applicable-field completeness."""
    quality = quality_fields(row)
    fields = quality.get("field_status") or {}
    filled = [item.get("label", key) for key, item in fields.items()
              if item.get("status") == "captured"]
    # Queue UI currently displays 20-point buckets; keep the bucket contract while
    # the product card exposes the exact percentage and numerator/denominator.
    score = int(round(float(quality.get("percent", 0)) / 20) * 20)
    return max(0, min(100, score)), filled


def enrichment_status(params: dict | None = None) -> dict:
    """Counts enrichment status for the selected store chain only."""
    chain = request_chain(params or {})
    conn = connect_db()
    try:
        products = conn.execute(
            "SELECT sap_code,plu,image_url,image_file FROM products WHERE chain=?",
            (chain,),
        ).fetchall()
        missing_main = 0
        for row in products:
            if not row[2]:
                continue
            local = row[3]
            if not local or not (BASE_DIR / str(local)).is_file():
                missing_main += 1

        images = conn.execute(
            """SELECT image_url,local_file FROM product_images
               WHERE chain=? AND image_url IS NOT NULL AND image_url!=''""",
            (chain,),
        ).fetchall()
        missing_gallery = 0
        for row in images:
            local = row[1]
            if not local or not (BASE_DIR / str(local)).is_file():
                missing_gallery += 1

        detail_rows = conn.execute(
            "SELECT * FROM product_details WHERE chain=?",
            (chain,),
        ).fetchall()
        detail_keys = {(str(r["sap_code"]), str(r["plu"])) for r in detail_rows}
        products_without_details = sum(
            1 for r in products
            if (str(r[0]), str(r[1])) not in detail_keys
        )

        counts = {"pending": 0, "opened": 0, "exported": 0, "processed": 0, "skipped": 0}
        for row in conn.execute(
            "SELECT status,COUNT(*) n FROM enrichment_tasks WHERE chain=? GROUP BY status",
            (chain,),
        ):
            counts[row["status"]] = int(row["n"])

        scores = [_detail_coverage(dict(r))[0] for r in detail_rows]
        return {
            "chain": chain,
            "total_products": len(products),
            "main_photos_missing": missing_main,
            "gallery_images_total": len(images),
            "gallery_images_missing": missing_gallery,
            "products_with_details": sum(1 for x in scores if x > 0),
            "products_without_details": products_without_details,
            "products_with_empty_details": sum(1 for x in scores if x == 0),
            "average_detail_percent": round(sum(scores) / max(1, len(products)), 1),
            "task_counts": counts,
        }
    finally:
        conn.close()


def enrichment_queue(params: dict) -> dict:
    """Mass-enrichment queue with category, status and data-completeness filters."""
    conn = connect_db()
    try:
        q = (params.get("q") or [""])[0].strip()[:100]
        category = (params.get("category") or [""])[0].strip()[:160]
        status_filter = (params.get("status") or ["pending"])[0].strip()
        completeness = (params.get("completeness") or ["incomplete"])[0].strip()
        limit = max(10, min(int((params.get("limit") or ["25"])[0]), 100))
        offset = max(0, int((params.get("offset") or ["0"])[0]))
        chain = request_chain(params)

        rows = conn.execute("""
            SELECT p.chain,p.sap_code,p.plu,p.name,p.categories,p.product_url,p.current_price,p.image_url,p.image_file,
                   p.first_seen_at,p.last_seen_at,
                   d.composition,d.description,d.nutrition_json,d.attributes_json,d.details_text,
                   CASE WHEN d.plu IS NOT NULL THEN 1 ELSE 0 END AS has_details_record,
                   COALESCE(t.status,'pending') AS task_status,t.created_at AS task_created_at,
                   t.last_action_at,t.opened_at,t.exported_at,t.processed_at,t.skipped_at,t.note
            FROM products p
            LEFT JOIN product_details d ON d.chain=p.chain AND d.sap_code=p.sap_code AND d.plu=p.plu
            LEFT JOIN enrichment_tasks t ON t.chain=p.chain AND t.sap_code=p.sap_code AND t.plu=p.plu
            WHERE p.chain=?
            ORDER BY p.name COLLATE NOCASE
        """, (chain,)).fetchall()
        category_map: dict[str, set[str]] = {}
        for cr in conn.execute("SELECT category,plu FROM product_categories WHERE chain=? ORDER BY category", (chain,)):
            category_map.setdefault(str(cr[1]), set()).add(str(cr[0]))
        category_set = sorted({c for values in category_map.values() for c in values}, key=str.casefold)
        all_items = []
        for raw in rows:
            item = dict(raw)
            item["chain"] = chain
            score, filled = _detail_coverage(item)
            item["detail_percent"] = score
            item["detail_blocks"] = filled
            item["has_details_record"] = bool(item.get("has_details_record"))
            item["has_details"] = score > 0
            item["quality"] = quality_fields(item)
            item["task_status"] = item.get("task_status") or "pending"
            item["category_list"] = sorted(category_map.get(str(item["plu"]), set()), key=str.casefold)
            if not item["category_list"] and item.get("categories"):
                item["category_list"] = [x.strip() for x in str(item["categories"]).split(",") if x.strip()]
            item["local_image"] = f"/photos/{Path(str(item['image_file'])).name}" if item.get("image_file") else None
            all_items.append(item)

        status_counts = {k: 0 for k in ("pending", "opened", "exported", "processed", "skipped")}
        for item in all_items:
            status_counts[item["task_status"]] = status_counts.get(item["task_status"], 0) + 1
        total_products = len(all_items)
        # Summary progress reflects actual product_details records, not queue actions.
        processed_count = sum(1 for x in all_items if x["has_details_record"])
        pending_details_count = total_products - processed_count
        coverage_counts = {
            str(percent): sum(1 for x in all_items if x["detail_percent"] == percent)
            for percent in (100, 80, 60, 40, 20, 0)
        }
        summary = {
            "total": total_products, "processed": processed_count,
            "pending": pending_details_count, "opened": status_counts.get("opened", 0),
            "exported": status_counts.get("exported", 0), "skipped": status_counts.get("skipped", 0),
            "with_any_details": sum(1 for x in all_items if x["detail_percent"] > 0),
            "without_details": sum(1 for x in all_items if x["detail_percent"] == 0),
            "average_percent": round(sum(x["detail_percent"] for x in all_items) / max(1, total_products), 1),
            "processed_percent": round(processed_count / max(1, total_products) * 100, 1),
            "coverage_counts": coverage_counts,
            "status_counts": status_counts,
        }

        # Category coverage uses distinct PLUs, not file row counts.
        category_totals: dict[str, set[str]] = {}
        category_done: dict[str, set[str]] = {}
        for item in all_items:
            for cat in item["category_list"]:
                category_totals.setdefault(cat, set()).add(item["plu"])
                if item["has_details_record"]:
                    category_done.setdefault(cat, set()).add(item["plu"])
        category_progress = []
        for cat in category_set:
            count = len(category_totals.get(cat, set()))
            done = len(category_done.get(cat, set()))
            category_progress.append({"category": cat, "total": count, "processed": done,
                                      "percent": round(done / max(1, count) * 100, 1)})

        filtered = all_items
        if q:
            ql = q.casefold()
            filtered = [x for x in filtered if ql in str(x.get("name") or "").casefold()
                        or ql in str(x.get("plu") or "").casefold()
                        or ql in str(x.get("categories") or "").casefold()]
        if category:
            filtered = [x for x in filtered if category in x["category_list"]]
        if status_filter and status_filter != "all":
            filtered = [x for x in filtered if x["task_status"] == status_filter]
        if completeness == "none":
            filtered = [x for x in filtered if x["detail_percent"] == 0]
        elif completeness == "partial":
            filtered = [x for x in filtered if 0 < x["detail_percent"] < 100]
        elif completeness == "complete":
            filtered = [x for x in filtered if x["detail_percent"] == 100]
        elif completeness == "incomplete":
            filtered = [x for x in filtered if x["detail_percent"] < 100]
        # Work queue priority: pending first, then opened/exported; within each group least-filled first.
        rank = {"pending": 0, "opened": 1, "exported": 2, "processed": 3, "skipped": 4}
        filtered.sort(key=lambda x: (rank.get(x["task_status"], 9), x["detail_percent"],
                                     str(x.get("name") or "").casefold()))
        total = len(filtered)
        items = filtered[offset:offset + limit]
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "categories": category_set, "summary": summary, "category_progress": category_progress,
                "filters": {"q": q, "category": category, "status": status_filter, "completeness": completeness}}
    finally:
        conn.close()


def update_enrichment_task(data: dict) -> dict:
    chain = request_chain(data)
    sap = str(data.get("sap_code") or get_store(chain)["default_sap"]).strip()
    plu = str(data.get("plu") or "").strip()
    action = str(data.get("action") or "").strip().lower()
    validate_store(chain, sap)
    if not plu: raise ValueError("Не указан PLU")
    action_map = {"opened": "opened", "exported": "exported", "skip": "skipped", "reset": "pending"}
    if action not in action_map: raise ValueError("Неизвестное действие очереди")
    status = action_map[action]
    stamp = now_iso()
    conn = connect_db()
    try:
        exists = conn.execute(
            "SELECT 1 FROM products WHERE chain=? AND sap_code=? AND plu=?", (chain, sap, plu)
        ).fetchone()
        if not exists: raise KeyError("Товар не найден")
        conn.execute("""
            INSERT OR IGNORE INTO enrichment_tasks(chain,sap_code,plu,status,created_at,last_action_at)
            VALUES(?,?,?,'pending',?,?)
        """, (chain, sap, plu, stamp, stamp))
        updates = ["status=?", "last_action_at=?", "note=?"]
        values: list = [status, stamp, str(data.get("note") or "")[:300]]
        if action == "opened": updates.append("opened_at=COALESCE(opened_at,?)"); values.append(stamp)
        elif action == "exported": updates.append("exported_at=?"); values.append(stamp)
        elif action == "skip": updates.append("skipped_at=?"); values.append(stamp)
        elif action == "reset": updates.extend(["skipped_at=NULL"])
        values.extend([chain, sap, plu])
        conn.execute(f"UPDATE enrichment_tasks SET {','.join(updates)} WHERE chain=? AND sap_code=? AND plu=?", values)
        conn.execute("INSERT INTO enrichment_task_events(chain,sap_code,plu,action,created_at,note) VALUES(?,?,?,?,?,?)",
                     (chain, sap, plu, action, stamp, str(data.get("note") or "")[:300]))
        conn.commit()
        return {"chain": chain, "sap_code": sap, "plu": plu, "status": status, "updated_at": stamp}
    finally:
        conn.close()


def _batch_row(conn: sqlite3.Connection, chain: str = DEFAULT_CHAIN) -> dict:
    chain = normalize_chain(chain)
    conn.execute("INSERT OR IGNORE INTO enrichment_batch_states(chain) VALUES(?)", (chain,))
    row = conn.execute(
        "SELECT * FROM enrichment_batch_states WHERE chain=?", (chain,)
    ).fetchone()
    state = dict(row) if row else {
        "chain": chain, "active": 0, "max_items": 25, "delay_seconds": 15,
        "processed_count": 0, "category": None, "current_sap": None,
        "current_plu": None, "current_url": None, "started_at": None,
        "updated_at": None, "paused_reason": None, "last_error": None,
    }
    state["current_chain"] = chain
    return state


def _batch_pick_next(category: str | None = None, chain: str = DEFAULT_CHAIN) -> dict | None:
    params = {"status": ["all"], "completeness": ["incomplete"], "limit": ["100"], "offset": ["0"],
              "category": [category or ""], "q": [""], "chain": [chain]}
    data = enrichment_queue(params)
    prefix = product_url_prefix(chain)
    for item in data.get("items", []):
        url = str(item.get("product_url") or "")
        if item.get("task_status") not in {"pending", "opened", "exported"}:
            continue
        if url.startswith(prefix) and str(item.get("plu") or "").isdigit():
            return item
    return None


def _batch_reserve_item(item: dict) -> dict:
    chain = normalize_chain(item.get("chain") or DEFAULT_CHAIN)
    sap = str(item.get("sap_code") or get_store(chain)["default_sap"])
    plu = str(item.get("plu") or "")
    url = str(item.get("product_url") or "")
    stamp = now_iso()
    conn = connect_db()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO enrichment_tasks(chain,sap_code,plu,status,created_at,last_action_at)
            VALUES(?,?,?,'pending',?,?)
        """, (chain, sap, plu, stamp, stamp))
        conn.execute("""
            UPDATE enrichment_tasks SET status='opened', last_action_at=?, opened_at=COALESCE(opened_at,?),
                note='Пакетный режим V14: карточка открыта в обычном браузере'
            WHERE chain=? AND sap_code=? AND plu=? AND status!='processed'
        """, (stamp, stamp, chain, sap, plu))
        conn.execute("INSERT OR IGNORE INTO enrichment_batch_states(chain) VALUES(?)", (chain,))
        conn.execute("""
            UPDATE enrichment_batch_states
            SET current_sap=?,current_plu=?,current_url=?,updated_at=?,last_error=NULL
            WHERE chain=?
        """, (sap, plu, url, stamp, chain))
        conn.commit()
        return {"chain": chain, "sap_code": sap, "plu": plu, "name": item.get("name"), "url": url}
    finally:
        conn.close()


def batch_status(chain: str | None = None) -> dict:
    active_chain = normalize_chain(chain or DEFAULT_CHAIN)
    conn = connect_db()
    try:
        state = _batch_row(conn, active_chain)
        prefix = product_url_prefix(active_chain)
        total = int(conn.execute(
            "SELECT COUNT(*) FROM products WHERE chain=?", (active_chain,)
        ).fetchone()[0])
        done = int(conn.execute(
            "SELECT COUNT(*) FROM enrichment_tasks WHERE chain=? AND status='processed'", (active_chain,)
        ).fetchone()[0])
        remaining = int(conn.execute("""
            SELECT COUNT(*) FROM products p
            LEFT JOIN enrichment_tasks t ON t.chain=p.chain AND t.sap_code=p.sap_code AND t.plu=p.plu
            WHERE p.chain=? AND p.product_url LIKE ?
              AND COALESCE(t.status,'pending') NOT IN ('processed','skipped')
        """, (active_chain, prefix + "%")).fetchone()[0])
        state.update({"active": bool(state.get("active")), "chain": active_chain,
                      "current_chain": active_chain,
                      "total_products": total, "processed_total": done, "remaining_total": remaining,
                      "batch_progress_percent": round(int(state.get("processed_count") or 0) / max(1, int(state.get("max_items") or 1)) * 100, 1)})
        return state
    finally:
        conn.close()


def batch_start(data: dict) -> dict:
    limit = max(1, min(int(data.get("limit") or 25), 200))
    delay = max(12, min(int(data.get("delay_seconds") or 15), 60))
    category = str(data.get("category") or "").strip()[:160] or None
    chain = request_chain(data)
    if not DB_PATH.exists(): raise FileNotFoundError(f"Не найдена база {DB_PATH.name}")
    current = batch_status(chain)
    if current.get("active"):
        raise ValueError("Пакетный сбор уже выполняется. Сначала приостановите его.")
    item = _batch_pick_next(category, chain)
    if not item:
        raise ValueError("В выбранной категории нет ожидающих товаров с URL карточки. Проверьте фильтр или статус очереди.")
    stamp = now_iso()
    conn = connect_db()
    try:
        conn.execute("INSERT OR IGNORE INTO enrichment_batch_states(chain) VALUES(?)", (chain,))
        conn.execute("""UPDATE enrichment_batch_states SET active=1,max_items=?,delay_seconds=?,processed_count=0,
            category=?,started_at=?,updated_at=?,paused_reason=NULL,last_error=NULL WHERE chain=?""",
                     (limit, delay, category, stamp, stamp, chain))
        conn.commit()
    finally:
        conn.close()
    reserved = _batch_reserve_item(item)
    return {"started": True, "limit": limit, "delay_seconds": delay, "category": category,
            "chain": chain, "current": reserved,
            "first_url": reserved["url"], "status": batch_status(chain)}


def batch_pause(reason: str = "Остановлено пользователем", chain: str = DEFAULT_CHAIN) -> dict:
    chain = normalize_chain(chain)
    stamp = now_iso()
    conn = connect_db()
    try:
        conn.execute("INSERT OR IGNORE INTO enrichment_batch_states(chain) VALUES(?)", (chain,))
        conn.execute("UPDATE enrichment_batch_states SET active=0,updated_at=?,paused_reason=? WHERE chain=?",
                     (stamp, str(reason or "Остановлено пользователем")[:300], chain))
        conn.commit()
    finally:
        conn.close()
    return batch_status(chain)


def batch_resume(chain: str = DEFAULT_CHAIN) -> dict:
    chain = normalize_chain(chain)
    state = batch_status(chain)
    if state.get("active"):
        return {"resumed": True, "current_url": state.get("current_url"), "status": state}
    current_url = state.get("current_url")
    current_plu = state.get("current_plu")
    starting_next_package = not (current_url and current_plu)
    if starting_next_package:
        item = _batch_pick_next(state.get("category"), chain)
        if not item:
            raise ValueError("В очереди больше нет ожидающих карточек.")
        current = _batch_reserve_item(item)
        current_url = current["url"]
    stamp = now_iso()
    conn = connect_db()
    try:
        if starting_next_package:
            # A completed 200-item batch starts a fresh counter on Continue.
            conn.execute("""UPDATE enrichment_batch_states SET active=1,processed_count=0,
                updated_at=?,paused_reason=NULL,last_error=NULL WHERE chain=?""", (stamp, chain))
        else:
            # CAPTCHA/manual pause resumes the same reserved product and keeps the counter.
            conn.execute("UPDATE enrichment_batch_states SET active=1,updated_at=?,paused_reason=NULL,last_error=NULL WHERE chain=?", (stamp, chain))
        conn.commit()
    finally:
        conn.close()
    return {"resumed": True, "current_url": current_url, "status": batch_status(chain)}


def batch_fail(reason: str, captcha: bool = False, chain: str = DEFAULT_CHAIN) -> dict:
    chain = normalize_chain(chain)
    reason = str(reason or ("Обнаружена проверка сайта" if captcha else "Ошибка страницы"))[:500]
    stamp = now_iso()
    conn = connect_db()
    try:
        conn.execute("INSERT OR IGNORE INTO enrichment_batch_states(chain) VALUES(?)", (chain,))
        conn.execute("UPDATE enrichment_batch_states SET active=0,updated_at=?,paused_reason=?,last_error=? WHERE chain=?",
                     (stamp, "CAPTCHA — требуется ручная проверка" if captcha else "Пауза из-за ошибки страницы", reason, chain))
        conn.commit()
    finally:
        conn.close()
    return batch_status(chain)


def batch_record(payload: dict) -> dict:
    """Accept one product snapshot exported from the visible product page; never call 5ka APIs."""
    if not isinstance(payload, dict) or payload.get("source") not in all_sources():
        raise ValueError("Неверный формат подробной карточки")
    chain = chain_from_payload(payload)
    store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
    sap = str(store.get("sap_code") or "").strip()
    products = payload.get("products") if isinstance(payload.get("products"), list) else [payload.get("product")]
    products = [x for x in products if isinstance(x, dict)]
    if not products:
        raise ValueError("В карточке отсутствует блок product")
    validate_chain_url(chain, payload.get("source_url"))
    for product in products:
        validate_chain_url(chain, product.get("product_url"))
    validate_store(chain, sap)
    plu = str(products[0].get("plu") or "").strip()
    if not plu or not plu.isdigit():
        raise ValueError("В карточке отсутствует корректный PLU")
    state = batch_status(chain)
    if not state.get("active"):
        raise ValueError("Пакетный сбор приостановлен. Возобновите его в Catalog Desk.")
    if str(state.get("current_chain") or chain) != chain:
        raise ValueError(f"Ожидается пакет сети {state.get('current_chain')}, получен {chain}")
    if str(state.get("current_sap") or sap) != sap:
        raise ValueError(f"Ожидается SAP {state.get('current_sap')}, получен {sap}. Запись не принята.")
    if str(state.get("current_plu") or "") != plu:
        raise ValueError(f"Ожидался PLU {state.get('current_plu')}, получен {plu}. Запись не принята.")
    detail_dir = chain_path(chain, "details")
    detail_dir.mkdir(parents=True, exist_ok=True)
    import uuid
    safe_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = detail_dir / f"batch_detail_{chain}_{sap}_{plu}_{safe_stamp}_{uuid.uuid4().hex[:6]}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    imported_files, imported_products, skipped, errors = import_pending_details()
    if imported_products < 1:
        batch_fail("JSON сохранён, но импорт подробностей не подтвердился: " + "; ".join(errors[:2]), chain=chain)
        raise ValueError("Не удалось импортировать подробную карточку. Проверьте журнал импорта.")
    conn = connect_db()
    try:
        conn.execute("""UPDATE enrichment_batch_states SET processed_count=processed_count+1,updated_at=?,
            current_sap=NULL,current_plu=NULL,current_url=NULL,last_error=NULL WHERE chain=?""", (now_iso(), chain))
        conn.commit()
    finally:
        conn.close()
    new_state = batch_status(chain)
    if int(new_state.get("processed_count") or 0) >= int(new_state.get("max_items") or 1):
        batch_pause("Пакет завершён: достигнут установленный лимит", chain)
        return {"accepted": True, "plu": plu, "processed_count": new_state["processed_count"],
                "batch_finished": True, "next_url": None, "status": batch_status(chain)}
    next_item = _batch_pick_next(new_state.get("category"), chain)
    if not next_item:
        batch_pause("Пакет завершён: ожидающих карточек больше нет", chain)
        return {"accepted": True, "plu": plu, "processed_count": new_state["processed_count"],
                "batch_finished": True, "next_url": None, "status": batch_status(chain)}
    reserved = _batch_reserve_item(next_item)
    return {"accepted": True, "plu": plu, "processed_count": new_state["processed_count"],
            "batch_finished": False, "next_url": reserved["url"], "next": reserved,
            "delay_seconds": int(new_state.get("delay_seconds") or 15), "status": batch_status(chain)}

def _download_catalog_image(url: str, destination: Path, chain: str = DEFAULT_CHAIN) -> bool:
    """Download only product image URLs already present in the local database."""
    from urllib.parse import urlparse
    import requests
    from PIL import Image
    store = get_store(normalize_chain(chain))
    host = (urlparse(url).hostname or "").lower()
    allowed = [str(h).lower() for h in store["image_hosts"]]
    if urlparse(url).scheme != "https" or not any(host == h or host.endswith("." + h) for h in allowed):
        return False
    destination.parent.mkdir(parents=True,exist_ok=True)
    try:
        response=requests.get(url,timeout=25,headers={"Referer":store["referer"],"User-Agent":"Mozilla/5.0"})
        response.raise_for_status()
        with Image.open(BytesIO(response.content)) as img:
            if img.mode in ("RGBA","LA","P"):
                rgba=img.convert("RGBA")
                bg=Image.new("RGB",rgba.size,"white")
                bg.paste(rgba,mask=rgba.getchannel("A"))
                out=bg
            else:
                out=img.convert("RGB")
            out.save(destination,"JPEG",quality=90,optimize=True)
        return True
    except Exception:
        return False


def _job_begin(kind: str, message: str) -> int | None:
    global _JOB
    with _JOB_LOCK:
        if _JOB["running"]:
            return None
        _JOB["running"] = True
        _JOB["job_id"] += 1
        job_id=_JOB["job_id"]
        _JOB.update({"kind":kind,"started_at":now_iso(),"finished_at":None,"return_code":None,
                     "output":message,"error":None})
        return job_id


def _job_write(job_id: int, message: str) -> None:
    with _JOB_LOCK:
        if _JOB["job_id"]==job_id:
            _JOB["output"]=(str(_JOB.get("output") or "")+message)[-24000:]


def _job_end(job_id: int, return_code: int=0, error: str|None=None) -> None:
    global _JOB
    with _JOB_LOCK:
        if _JOB["job_id"]==job_id:
            _JOB.update({"running":False,"finished_at":now_iso(),"return_code":return_code,"error":error})


def _refresh_exports_without_photos(job_id: int) -> int:
    """Rebuild Excel/CSV after an enrichment task without downloading photos."""
    env=os.environ.copy(); env["PYTHONIOENCODING"]="utf-8"; env["PYTHONUTF8"]="1"; env["PYTHONUNBUFFERED"]="1"
    proc=subprocess.Popen([sys.executable,str(IMPORTER_PATH),"--no-photos"],cwd=str(BASE_DIR),
                          stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        _job_write(job_id,line)
    return proc.wait()


def run_details_import() -> None:
    job_id=_job_begin("details","Импортирую дополнительные карточки товаров…\n")
    if job_id is None: return
    try:
        files,products,skipped,errors=import_pending_details()
        _job_write(job_id,f"Доп. сведения: файлов импортировано {files}; карточек обновлено {products}; повторов пропущено {skipped}.\n")
        if errors: _job_write(job_id,"Ошибки: "+"; ".join(errors[:10])+"\n")
        _job_write(job_id,"Обновляю Excel и отчёт…\n")
        rc=_refresh_exports_without_photos(job_id)
        _job_end(job_id,rc,None if rc==0 else f"Пересборка Excel завершилась с кодом {rc}")
    except Exception as exc:
        _job_end(job_id,-1,str(exc))


def run_photo_backfill() -> None:
    job_id=_job_begin("photos","Проверяю каталог и загружаю недостающие фотографии…\n")
    if job_id is None: return
    conn=None
    try:
        PHOTO_DIR.mkdir(parents=True,exist_ok=True)
        conn=connect_db()
        rows=conn.execute("SELECT chain,sap_code,plu,image_url,image_file FROM products WHERE image_url IS NOT NULL AND image_url!='' ORDER BY name COLLATE NOCASE").fetchall()
        main_total=len(rows); main_done=0; main_failed=0
        for i,row in enumerate(rows,1):
            chain,sap,plu,url,local=row
            photo_dir=BASE_DIR / get_store(normalize_chain(chain))["paths"]["photos"]
            photo_dir.mkdir(parents=True,exist_ok=True)
            local_path=BASE_DIR/str(local) if local else None
            if local_path and local_path.is_file():
                main_done+=1
            else:
                safe_sap=re.sub(r"[^A-Za-z0-9_-]","_",str(sap))
                safe_plu=re.sub(r"[^A-Za-z0-9_-]","_",str(plu))
                legacy=photo_dir/f"{safe_plu}.jpg"
                dest=legacy if legacy.exists() and legacy.stat().st_size>0 else photo_dir/f"{safe_sap}_{safe_plu}.jpg"
                if (dest.exists() and dest.stat().st_size>0) or _download_catalog_image(str(url),dest,chain):
                    rel=str(dest.relative_to(BASE_DIR))
                    conn.execute("UPDATE products SET image_file=? WHERE chain=? AND sap_code=? AND plu=?",(rel,chain,sap,plu))
                    conn.execute("UPDATE product_images SET local_file=? WHERE chain=? AND sap_code=? AND plu=? AND image_url=?",(rel,chain,sap,plu,url))
                    main_done+=1
                else: main_failed+=1
            if i%25==0:
                conn.commit(); _job_write(job_id,f"Основные фото: {i}/{main_total}; успешно {main_done}; ошибок {main_failed}.\n")
                time.sleep(0.15)
        conn.commit()
        gallery=conn.execute("SELECT id,chain,sap_code,plu,image_url,local_file,image_order FROM product_images ORDER BY chain,sap_code,plu,image_order,id").fetchall()
        gallery_total=len(gallery); gallery_done=0; gallery_failed=0
        for i,row in enumerate(gallery,1):
            image_id,chain,sap,plu,url,local,order=row
            photo_dir=BASE_DIR / get_store(normalize_chain(chain))["paths"]["photos"]
            photo_dir.mkdir(parents=True,exist_ok=True)
            local_path=BASE_DIR/str(local) if local else None
            if local_path and local_path.is_file():
                gallery_done+=1; continue
            product=conn.execute("SELECT image_url,image_file FROM products WHERE chain=? AND sap_code=? AND plu=?",(chain,sap,plu)).fetchone()
            if product and product[0]==url and product[1] and (BASE_DIR/str(product[1])).is_file():
                rel=str(product[1]); conn.execute("UPDATE product_images SET local_file=? WHERE id=?",(rel,image_id)); gallery_done+=1; continue
            suffix=__import__('hashlib').sha1(str(url).encode('utf-8')).hexdigest()[:8]
            safe_sap=re.sub(r"[^A-Za-z0-9_-]","_",str(sap)); safe_plu=re.sub(r"[^A-Za-z0-9_-]","_",str(plu))
            dest=photo_dir/f"{safe_sap}_{safe_plu}_g{suffix}.jpg"
            if (dest.exists() and dest.stat().st_size>0) or _download_catalog_image(str(url),dest,chain):
                conn.execute("UPDATE product_images SET local_file=? WHERE id=?",(str(dest.relative_to(BASE_DIR)),image_id)); gallery_done+=1
            else: gallery_failed+=1
            if i%25==0:
                conn.commit(); _job_write(job_id,f"Галерея: {i}/{gallery_total}; успешно {gallery_done}; ошибок {gallery_failed}.\n")
                time.sleep(0.15)
        conn.commit()
        _job_write(job_id,f"Фото завершены. Основные: {main_done}/{main_total}, ошибок {main_failed}. Галерея: {gallery_done}/{gallery_total}, ошибок {gallery_failed}.\n")
        conn.close(); conn=None
        _job_write(job_id,"Обновляю Excel и отчёт…\n")
        rc=_refresh_exports_without_photos(job_id)
        _job_end(job_id,rc,None if rc==0 else f"Пересборка Excel завершилась с кодом {rc}")
    except Exception as exc:
        if conn:
            try: conn.close()
            except Exception: pass
        _job_end(job_id,-1,str(exc))

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
    for cfg in STORES.values():
        for folder in cfg["paths"].values():
            (BASE_DIR / folder).mkdir(parents=True, exist_ok=True)
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
        source = str(payload.get("source") or "") if isinstance(payload, dict) else ""
        chain = chain_from_payload(payload)
        if source.endswith("visible-category-links") and isinstance(payload.get("categories"), list):
            dest_dir, kind = chain_path(chain, "categories"), "categories"
        elif source.endswith("visible-product-details-export") or source.endswith("visible-product-detail-export"):
            if not (payload.get("product") or payload.get("products")):
                raise ValueError(f"{name}: нет блока product/products")
            store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
            sap = str(store.get("sap_code") or payload.get("sap_code") or get_store(chain)["default_sap"]).strip()
            validate_store(chain, sap)
            dest_dir, kind = chain_path(chain, "details"), "details"
        elif isinstance(payload, dict) and isinstance(payload.get("products"), list) and payload.get("products"):
            store = payload.get("store") if isinstance(payload.get("store"), dict) else {}
            sap = store.get("sap_code") or payload.get("store_sap_code")
            if sap:
                validate_store(chain, str(sap).strip())
            dest_dir, kind = chain_path(chain, "exports"), "products"
        else:
            raise ValueError(f"{name}: это не экспорт товаров, доп. сведений или категорий")
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


def run_import(download_photos: bool = False, chain: str = DEFAULT_CHAIN) -> None:
    global _JOB
    with _JOB_LOCK:
        if _JOB["running"]: return
        _JOB["running"] = True
        _JOB["job_id"] += 1
        job_id = _JOB["job_id"]
        _JOB.update({"kind":"catalog_import", "started_at": now_iso(), "finished_at": None, "return_code": None,
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
        cmd = [sys.executable, str(IMPORTER_PATH), "--chain", normalize_chain(chain)]
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
        conn = connect_db()
        try:
            conn.execute("""
                UPDATE daily_runs SET status=?,finished_at=?
                WHERE chain=? AND run_date=? AND status='pending'
            """, ("done" if return_code == 0 else "failed", now_iso(), normalize_chain(chain), now_iso()[:10]))
            conn.commit()
        finally:
            conn.close()
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
    server_version = "PyaterochkaCatalogUI/14.0"

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
                photo_path = PHOTO_DIR / name
                for photo_dir in store_dirs("photos"):
                    candidate = photo_dir / name
                    if candidate.is_file():
                        photo_path = candidate
                        break
                return self._file(photo_path)
            if path == "/files/excel": return self._file(BASE_DIR / "pyaterochka_catalog_v9.xlsx", attachment=True)
            if path == "/files/csv": return self._file(BASE_DIR / "pyaterochka_catalog_v9.csv", attachment=True)
            if path == "/api/health": return self._json(200, {"ok": True, "db_exists": DB_PATH.exists(), "db": DB_PATH.name, "version": "16.1.0-dev", "chains": list(STORES)})
            if path == "/api/stores": return self._json(200, stores_data())
            if path == "/api/compare": return self._json(200, compare_data(params))
            if path == "/api/search": return self._json(200, search_products(params))
            if path == "/api/ingredients": return self._json(200, ingredients_data(params))
            if path == "/api/compare-table": return self._json(200, compare_table_data(params))
            if path.startswith("/api/v1/"): return self._json(200, api_v1(path, params))
            if path == "/api/errors": return self._json(200, errors_data(params))
            if path == "/api/schedule": return self._json(200, schedule_data())
            if path == "/api/dashboard": return self._json(200, dashboard_data(params))
            if path == "/api/analytics": return self._json(200, analytics_data(params))
            if path == "/api/products": return self._json(200, products_data(params))
            if path == "/api/product": return self._json(200, product_detail(params))
            if path == "/api/favorites": return self._json(200, favorites_data(params))
            if path == "/api/changes":
                conn = connect_db()
                try:
                    direction = (params.get("direction") or ["all"])[0]
                    limit = int((params.get("limit") or ["300"])[0])
                    return self._json(200, get_price_changes(conn, limit, direction, chain=request_chain(params)))
                finally: conn.close()
            if path == "/api/categories":
                conn = connect_db()
                try: return self._json(200, category_progress(conn, request_chain(params)))
                finally: conn.close()
            if path == "/api/exports": return self._json(200, exports_data())
            if path == "/api/pending": return self._json(200, pending_exports_data())
            if path == "/api/enrichment/status": return self._json(200, enrichment_status(params))
            if path == "/api/enrichment/queue": return self._json(200, enrichment_queue(params))
            if path == "/api/enrichment/overview":
                overview_params = dict(params)
                overview_params.update({"status": ["all"], "completeness": ["all"], "limit": ["10"], "offset": ["0"]})
                return self._json(200, enrichment_queue(overview_params))
            if path == "/api/details/refresh/claim": return self._json(200, claim_detail_refresh(params))
            if path == "/api/batch/status": return self._json(200, batch_status(request_chain(params)))
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
            if parsed.path == "/api/errors/retry":
                return self._json(200, retry_error(data))
            if parsed.path == "/api/schedule":
                return self._json(200, set_schedule(data))
            if parsed.path == "/api/backup":
                return self._json(200, backup_now())
            if parsed.path == "/api/upload":
                return self._json(200, upload_payload(data))
            if parsed.path == "/api/favorite":
                return self._json(200, set_favorite(data))
            if parsed.path == "/api/enrichment/task":
                return self._json(200, update_enrichment_task(data))
            if parsed.path == "/api/details/refresh/request":
                return self._json(200, create_detail_refresh_request(data))
            if parsed.path == "/api/details/refresh/record":
                return self._json(200, record_single_detail_refresh(data))
            if parsed.path == "/api/batch/start":
                return self._json(200, batch_start(data))
            if parsed.path == "/api/batch/pause":
                return self._json(200, batch_pause(
                    str(data.get("reason") or "Остановлено пользователем"), request_chain(data)
                ))
            if parsed.path == "/api/batch/resume":
                return self._json(200, batch_resume(request_chain(data)))
            if parsed.path == "/api/batch/fail":
                return self._json(200, batch_fail(
                    str(data.get("reason") or "Ошибка страницы"), bool(data.get("captcha")), request_chain(data)
                ))
            if parsed.path == "/api/batch/record":
                return self._json(200, batch_record(data.get("payload") or {}))
            if parsed.path == "/api/import":
                if not IMPORTER_PATH.exists(): raise FileNotFoundError(f"Не найден {IMPORTER_PATH.name}")
                download = bool(data.get("download_photos"))
                chain = request_chain(data)
                with _JOB_LOCK:
                    if _JOB["running"]: return self._json(409, {"error": "Импорт уже выполняется"})
                thread = threading.Thread(target=run_import, args=(download, chain), daemon=True)
                thread.start()
                return self._json(202, {"started": True, "download_photos": download})
            if parsed.path == "/api/photos/backfill":
                if not DB_PATH.exists(): raise FileNotFoundError(f"Не найдена база {DB_PATH.name}")
                with _JOB_LOCK:
                    if _JOB["running"]: return self._json(409, {"error":"Другая фоновая задача уже выполняется"})
                threading.Thread(target=run_photo_backfill,daemon=True).start()
                return self._json(202,{"started":True,"kind":"photos"})
            if parsed.path == "/api/details/import":
                if not DB_PATH.exists(): raise FileNotFoundError(f"Не найдена база {DB_PATH.name}")
                with _JOB_LOCK:
                    if _JOB["running"]: return self._json(409, {"error":"Другая фоновая задача уже выполняется"})
                threading.Thread(target=run_details_import,daemon=True).start()
                return self._json(202,{"started":True,"kind":"details"})
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


def backup_database_before_v14() -> Path | None:
    """Create a one-time byte-for-byte backup before V14 modifies the V9 database."""
    if not DB_PATH.exists():
        return None
    backup_path = BASE_DIR / "pyaterochka_catalog_v9.pre_v14_backup.sqlite3"
    if not backup_path.exists():
        shutil.copy2(DB_PATH, backup_path)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальный веб-интерфейс товарной базы Пятёрочки")
    parser.add_argument("--host", default="127.0.0.1", help="По умолчанию доступен только на этом компьютере")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("Безопасность: интерфейс разрешено запускать только на loopback (127.0.0.1).")
        return 2
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    CATEGORY_DIR.mkdir(parents=True, exist_ok=True)
    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    try:
        backup_path = backup_database_before_v14()
        if backup_path:
            print(f"Резервная копия исходной базы: {backup_path.name}")
        v15_backup = backup_before_v15(DB_PATH)
        if v15_backup:
            print(f"Резервная копия перед V10: {v15_backup.name}")
        ensure_ui_columns()
    except Exception as exc:
        print(f"Предупреждение: не удалось применить миграции V14/V15: {exc}")
    url = f"http://127.0.0.1:{args.port}/"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print("=" * 64)
    print("PYATEROCHKA CATALOG — локальный интерфейс V14.0")
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
