"""Миграции схемы Catalog Desk.

V9 → V10: добавляет измерение `chain` и расширяет ключи таблиц, не теряя данные.
Все операции аддитивные по смыслу, но SQLite требует пересоздания таблиц при
изменении PRIMARY KEY / UNIQUE — поэтому таблицы копируются внутри транзакции.

См. CD-ADR-002 и docs/development/v15/DESIGN_DATA.md.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

DEFAULT_CHAIN = "5ka"

# Таблицы, где товар идентифицируется парой (sap_code, plu).
# product_detail_imports / enrichment_batch_state / catalog_ui_meta / schema_meta
# не привязаны к товару — им chain не нужен.
TABLES_WITH_CHAIN = (
    "exports",
    "products",
    "price_history",
    "product_categories",
    "product_details",
    "product_detail_history",
    "product_images",
    "enrichment_tasks",
    "enrichment_task_events",
    "favorites",
)

INDEXES_V10 = (
    "CREATE INDEX IF NOT EXISTS idx_history_product_time ON price_history(chain,sap_code,plu,captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_history_time ON price_history(captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_exports_category_page ON exports(chain,sap_code,category,page_number)",
    "CREATE INDEX IF NOT EXISTS idx_product_detail_history_time ON product_detail_history(chain,sap_code,plu,captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_product_images_product ON product_images(chain,sap_code,plu,image_order)",
    "CREATE INDEX IF NOT EXISTS idx_enrichment_tasks_status ON enrichment_tasks(status,last_action_at)",
    "CREATE INDEX IF NOT EXISTS idx_enrichment_task_events_product ON enrichment_task_events(chain,sap_code,plu,created_at)",
)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Имена колонок таблицы."""
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Есть ли таблица в базе."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _v10_ddl(sql: str) -> str:
    """Преобразовать DDL V9 в DDL V10: chain + расширенные ключи."""
    # Keep foreign-key constraints intact. Parent tables are rebuilt before
    # dependent tables in TABLES_WITH_CHAIN; enforcement is restored afterwards.
    text = sql

    # Вставляем chain сразу после "CREATE TABLE <name> (".
    text = re.sub(
        r"(CREATE\s+TABLE\s+[^(]+\()",
        lambda m: m.group(1) + "\n            chain TEXT NOT NULL DEFAULT '5ka',",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    # Расширяем составные ключи.
    text = re.sub(
        r"PRIMARY\s+KEY\s*\(\s*sap_code\s*,\s*plu\s*,\s*category\s*\)",
        "PRIMARY KEY (chain, sap_code, plu, category)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"PRIMARY\s+KEY\s*\(\s*sap_code\s*,\s*plu\s*\)",
        "PRIMARY KEY (chain, sap_code, plu)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"UNIQUE\s*\(\s*source_hash\s*,\s*sap_code\s*,\s*plu\s*\)",
        "UNIQUE (source_hash, chain, sap_code, plu)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"UNIQUE\s*\(\s*sap_code\s*,\s*plu\s*,\s*image_url\s*\)",
        "UNIQUE (chain, sap_code, plu, image_url)",
        text,
        flags=re.IGNORECASE,
    )
    # Убираем висящие запятые перед закрывающей скобкой после удаления FK.
    text = re.sub(r",\s*\)", ")", text)

    # Временное имя, чтобы не конфликтовать с оригиналом до DROP.
    text = re.sub(
        r"(CREATE\s+TABLE\s+)(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)",
        lambda m: f"{m.group(1)}{m.group(2)}_v10tmp",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return text


def _missing_value(declaration: dict) -> object:
    """Значение для NOT NULL-колонки, которой не было в старой таблице."""
    if declaration["dflt_value"] is not None:
        return None  # SQLite подставит DEFAULT сам
    typ = str(declaration["type"] or "").upper()
    if any(word in typ for word in ("INT", "REAL", "FLOA", "DOUB", "NUM")):
        return 0
    return ""


def _rebuild_table(conn: sqlite3.Connection, table: str) -> None:
    """Пересоздать одну таблицу с chain, скопировав все строки."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row or not row[0]:
        return

    old_columns = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]
    new_ddl = _v10_ddl(str(row[0]))
    conn.execute(new_ddl)

    new_info = {
        str(r[1]): {
            "type": r[2],
            "notnull": bool(r[3]),
            "dflt_value": r[4],
        }
        for r in conn.execute(f"PRAGMA table_info({table}_v10tmp)")
    }
    copy_columns = [c for c in old_columns if c in new_info]
    fill_columns = [
        name
        for name, info in new_info.items()
        if name not in old_columns and info["notnull"] and info["dflt_value"] is None
    ]

    select_sql = ", ".join(f'"{c}"' for c in copy_columns) or "NULL"
    rows = conn.execute(f"SELECT {select_sql} FROM {table}").fetchall()

    all_columns = copy_columns + fill_columns
    if all_columns:
        cols_sql = ", ".join(f'"{c}"' for c in all_columns)
        placeholders = ", ".join("?" for _ in all_columns)
        payload = []
        for old_row in rows:
            values = list(old_row[: len(copy_columns)])
            for name in fill_columns:
                values.append(_missing_value(new_info[name]))
            payload.append(values)
        conn.executemany(
            f"INSERT INTO {table}_v10tmp ({cols_sql}) VALUES ({placeholders})", payload
        )

    conn.execute(f"DROP TABLE {table}")
    conn.execute(f'ALTER TABLE "{table}_v10tmp" RENAME TO "{table}"')


def migrate_v9_to_v10(conn: sqlite3.Connection) -> bool:
    """Идемпотентно перевести базу на схему V10.

    Returns:
        True, если были внесены изменения; False, если база уже V10.
    """
    if table_exists(conn, "products") and "chain" in table_columns(conn, "products"):
        return False

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in TABLES_WITH_CHAIN:
            if not table_exists(conn, table):
                continue
            if "chain" not in table_columns(conn, table):
                _rebuild_table(conn, table)

        for statement in INDEXES_V10:
            target = re.search(r"ON\s+(\w+)\(", statement, re.IGNORECASE)
            if target and table_exists(conn, target.group(1)):
                conn.execute(statement)

        if not table_exists(conn, "schema_meta"):
            conn.execute(
                "CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
        conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version','10') "
            "ON CONFLICT(key) DO UPDATE SET value='10'"
        )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"V10 migration produced {len(violations)} foreign-key violation(s)"
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def backup_before_v15(db_path: Path) -> Path | None:
    """Одноразовая копия базы перед миграцией V15."""
    path = Path(db_path)
    if not path.exists():
        return None
    dest = path.with_name(f"{path.stem}.pre_v15{path.suffix}")
    if dest.exists():
        return dest
    src = sqlite3.connect(str(path), timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest
