# -*- coding: utf-8 -*-
"""Read-only query model for the S24 retailer catalog.

This module never creates, migrates, or writes to a database. Every public
query opens SQLite in URI mode=ro and sets PRAGMA query_only=ON.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

RETAILERS = ("auchan", "globus", "dixy", "chizhik", "perekrestok", "5ka")
RETAILER_TITLES = {
    "auchan": "Ашан",
    "globus": "Глобус",
    "dixy": "Дикси",
    "chizhik": "Чижик",
    "perekrestok": "Перекрёсток",
    "5ka": "Пятёрочка",
}
REQUIRED_TABLES = {
    "retailer_catalog_meta",
    "retailer_catalog_imports",
    "retailer_catalog_products",
    "retailer_product_observations",
    "retailer_product_identity_candidates",
    "retailer_identity_resolution_events",
    "retailer_product_price_observations",
    "retailer_product_category_observations",
    "retailer_product_asset_observations",
}


def open_readonly(database: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite database in strict read-only/query-only mode."""
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Database file does not exist: {path}")
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("SQLite query_only could not be enabled")
    return conn


def _schema_state(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(REQUIRED_TABLES - present)
    if missing:
        return False, "S24 schema is not installed; missing: " + ", ".join(missing)
    row = conn.execute(
        "SELECT meta_value FROM retailer_catalog_meta WHERE meta_key='schema_version'"
    ).fetchone()
    if row is None:
        return False, "S24 schema version marker is missing"
    if str(row[0]) != "1":
        return False, f"Unsupported S24 schema version: {row[0]}"
    return True, None


def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0] if row else 0)


def _group_counts(conn: sqlite3.Connection, sql: str, key: str) -> dict[str, int]:
    return {str(row[key]): int(row["records"]) for row in conn.execute(sql).fetchall()}


def get_summary(database: str | Path) -> dict[str, Any]:
    """Return S24 aggregate counts without mutating the database."""
    with closing(open_readonly(database)) as conn:
        available, reason = _schema_state(conn)
        if not available:
            return {
                "available": False,
                "schema_version": None,
                "reason": reason,
                "retailers": [],
                "totals": {},
            }

        products = _group_counts(
            conn,
            "SELECT retailer_key, COUNT(*) AS records "
            "FROM retailer_catalog_products GROUP BY retailer_key",
            "retailer_key",
        )
        imports = {
            str(row["retailer_key"]): dict(row)
            for row in conn.execute(
                """
                SELECT retailer_key,
                       COUNT(*) AS import_count,
                       SUM(CASE WHEN import_status='committed' THEN 1 ELSE 0 END) AS committed_imports,
                       SUM(source_record_count) AS source_records,
                       SUM(committed_record_count) AS committed_records,
                       MAX(imported_at_utc) AS last_imported_at
                FROM retailer_catalog_imports
                GROUP BY retailer_key
                """
            ).fetchall()
        }
        observations = {
            (str(row["retailer_key"]), str(row["identity_status"])): int(row["records"])
            for row in conn.execute(
                """
                SELECT retailer_key, identity_status, COUNT(*) AS records
                FROM retailer_product_observations
                GROUP BY retailer_key, identity_status
                """
            ).fetchall()
        }
        prices = _group_counts(
            conn,
            """
            SELECT o.retailer_key, COUNT(*) AS records
            FROM retailer_product_price_observations p
            JOIN retailer_product_observations o USING(observation_id)
            GROUP BY o.retailer_key
            """,
            "retailer_key",
        )
        categories = _group_counts(
            conn,
            """
            SELECT o.retailer_key, COUNT(*) AS records
            FROM retailer_product_category_observations c
            JOIN retailer_product_observations o USING(observation_id)
            GROUP BY o.retailer_key
            """,
            "retailer_key",
        )
        assets = {
            (str(row["retailer_key"]), str(row["verification_status"])): int(row["records"])
            for row in conn.execute(
                """
                SELECT o.retailer_key, a.verification_status, COUNT(*) AS records
                FROM retailer_product_asset_observations a
                JOIN retailer_product_observations o USING(observation_id)
                GROUP BY o.retailer_key, a.verification_status
                """
            ).fetchall()
        }
        candidates = {
            (str(row["retailer_key"]), str(row["candidate_status"])): int(row["records"])
            for row in conn.execute(
                """
                SELECT o.retailer_key, c.candidate_status, COUNT(*) AS records
                FROM retailer_product_identity_candidates c
                JOIN retailer_product_observations o USING(observation_id)
                GROUP BY o.retailer_key, c.candidate_status
                """
            ).fetchall()
        }

        retailer_rows = []
        for key in RETAILERS:
            imp = imports.get(key, {})
            resolved = observations.get((key, "resolved"), 0)
            pending = observations.get((key, "pending"), 0)
            invalid = observations.get((key, "invalid"), 0)
            retailer_rows.append({
                "retailer_key": key,
                "title": RETAILER_TITLES[key],
                "import_count": int(imp.get("import_count") or 0),
                "committed_imports": int(imp.get("committed_imports") or 0),
                "source_records": int(imp.get("source_records") or 0),
                "committed_records": int(imp.get("committed_records") or 0),
                "last_imported_at": imp.get("last_imported_at"),
                "confirmed_products": products.get(key, 0),
                "observations": resolved + pending + invalid,
                "resolved_observations": resolved,
                "pending_observations": pending,
                "invalid_observations": invalid,
                "price_observations": prices.get(key, 0),
                "category_observations": categories.get(key, 0),
                "asset_observations": sum(v for (r, _), v in assets.items() if r == key),
                "verified_assets": assets.get((key, "verified_image"), 0),
                "unverified_assets": assets.get((key, "unverified"), 0),
                "identity_candidates": sum(v for (r, _), v in candidates.items() if r == key),
                "unverified_candidates": candidates.get((key, "unverified"), 0),
                "collision_candidates": candidates.get((key, "collision"), 0),
            })

        totals = {
            "imports": _count(conn, "SELECT COUNT(*) FROM retailer_catalog_imports"),
            "confirmed_products": _count(conn, "SELECT COUNT(*) FROM retailer_catalog_products"),
            "observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_observations"),
            "resolved_observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_observations WHERE identity_status='resolved'"),
            "pending_observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_observations WHERE identity_status='pending'"),
            "invalid_observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_observations WHERE identity_status='invalid'"),
            "price_observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_price_observations"),
            "category_observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_category_observations"),
            "asset_observations": _count(conn, "SELECT COUNT(*) FROM retailer_product_asset_observations"),
            "verified_assets": _count(conn, "SELECT COUNT(*) FROM retailer_product_asset_observations WHERE verification_status='verified_image'"),
            "unverified_assets": _count(conn, "SELECT COUNT(*) FROM retailer_product_asset_observations WHERE verification_status='unverified'"),
            "identity_candidates": _count(conn, "SELECT COUNT(*) FROM retailer_product_identity_candidates"),
            "unverified_candidates": _count(conn, "SELECT COUNT(*) FROM retailer_product_identity_candidates WHERE candidate_status='unverified'"),
            "collision_candidates": _count(conn, "SELECT COUNT(*) FROM retailer_product_identity_candidates WHERE candidate_status='collision'"),
        }
        return {
            "available": True,
            "schema_version": 1,
            "retailers": retailer_rows,
            "totals": totals,
        }


def _decode_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _attach_related(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    ids = [int(row["observation_id"]) for row in rows]
    marks = ",".join("?" for _ in ids)
    params = tuple(ids)

    prices: dict[int, list[dict[str, Any]]] = {item: [] for item in ids}
    for row in conn.execute(
        f"""
        SELECT observation_id, field_name, raw_value_json, value_state,
               parsed_decimal_text, parse_status, value_quality, semantic_role,
               currency_code, currency_status
        FROM retailer_product_price_observations
        WHERE observation_id IN ({marks})
        ORDER BY observation_id, field_name
        """,
        params,
    ).fetchall():
        item = dict(row)
        item["raw_value"] = _decode_json(item["raw_value_json"])
        prices[int(item["observation_id"])].append(item)

    categories: dict[int, list[dict[str, Any]]] = {item: [] for item in ids}
    for row in conn.execute(
        f"""
        SELECT observation_id, source_field, path_position, raw_label_json, label_text
        FROM retailer_product_category_observations
        WHERE observation_id IN ({marks})
        ORDER BY observation_id, source_field, path_position
        """,
        params,
    ).fetchall():
        item = dict(row)
        item["raw_label"] = _decode_json(item["raw_label_json"])
        categories[int(item["observation_id"])].append(item)

    assets: dict[int, list[dict[str, Any]]] = {item: [] for item in ids}
    for row in conn.execute(
        f"""
        SELECT asset_observation_id, observation_id, source_field, asset_url,
               url_classification, classification_method, verification_status
        FROM retailer_product_asset_observations
        WHERE observation_id IN ({marks})
        ORDER BY observation_id, asset_observation_id
        """,
        params,
    ).fetchall():
        assets[int(row["observation_id"])].append(dict(row))

    candidates: dict[int, list[dict[str, Any]]] = {item: [] for item in ids}
    for row in conn.execute(
        f"""
        SELECT candidate_id, observation_id, candidate_value, candidate_source,
               candidate_status, created_at_utc
        FROM retailer_product_identity_candidates
        WHERE observation_id IN ({marks})
        ORDER BY observation_id, candidate_id
        """,
        params,
    ).fetchall():
        candidates[int(row["observation_id"])].append(dict(row))

    for row in rows:
        key = int(row["observation_id"])
        row["price_fields"] = prices[key]
        row["categories"] = categories[key]
        row["assets"] = assets[key]
        row["identity_candidates"] = candidates[key]
    return rows


def list_observations(
    database: str | Path,
    *,
    retailer_key: str = "all",
    identity_status: str = "all",
    query: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List source observations (not deduplicated stable products)."""
    retailer_key = str(retailer_key or "all").strip().lower()
    identity_status = str(identity_status or "all").strip().lower()
    query = str(query or "").strip()[:160]
    if retailer_key not in {"all", *RETAILERS}:
        raise ValueError("Unknown retailer_key")
    if identity_status not in {"all", "resolved", "pending", "invalid"}:
        raise ValueError("Unknown identity_status")
    try:
        page = max(1, int(page))
        page_size = min(100, max(10, int(page_size)))
    except (TypeError, ValueError):
        raise ValueError("page and page_size must be integers") from None

    where = []
    args: list[Any] = []
    if retailer_key != "all":
        where.append("o.retailer_key=?")
        args.append(retailer_key)
    if identity_status != "all":
        where.append("o.identity_status=?")
        args.append(identity_status)
    if query:
        like = f"%{query}%"
        where.append(
            "(o.product_name LIKE ? OR o.brand LIKE ? OR "
            "o.source_external_product_id LIKE ? OR o.product_url LIKE ? OR "
            "o.category_slug LIKE ?)"
        )
        args.extend([like] * 5)
    clause = " WHERE " + " AND ".join(where) if where else ""

    with closing(open_readonly(database)) as conn:
        available, reason = _schema_state(conn)
        if not available:
            return {"available": False, "reason": reason, "items": [], "total": 0, "page": page, "page_size": page_size}
        total = _count(
            conn,
            """
            SELECT COUNT(*)
            FROM retailer_product_observations o
            LEFT JOIN retailer_catalog_imports i ON i.import_id=o.import_id
            """ + clause,
            tuple(args),
        )
        offset = (page - 1) * page_size
        selected = conn.execute(
            """
            SELECT o.observation_id, o.import_id, o.source_record_index, o.retailer_key,
                   o.source_retailer_label, o.catalog_product_id, o.identity_status,
                   o.source_external_product_id, o.source_external_id_type,
                   o.source_external_id_field, o.product_name, o.brand, o.product_url,
                   o.image_url, o.uom, o.category_slug, o.category_id,
                   o.scraped_at_raw, o.scraped_at_utc, o.scraped_at_status,
                   i.source_file_name, i.imported_at_utc
            FROM retailer_product_observations o
            LEFT JOIN retailer_catalog_imports i ON i.import_id=o.import_id
            """ + clause + """
            ORDER BY o.observation_id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(args) + (page_size, offset),
        ).fetchall()
        items = _attach_related(conn, [dict(row) for row in selected])
        return {
            "available": True,
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
        }


def get_observation(database: str | Path, observation_id: int) -> dict[str, Any] | None:
    """Return one observation, its source payload and related provenance rows."""
    try:
        observation_id = int(observation_id)
    except (TypeError, ValueError):
        raise ValueError("observation_id must be an integer") from None
    if observation_id < 1:
        raise ValueError("observation_id must be positive")

    with closing(open_readonly(database)) as conn:
        available, reason = _schema_state(conn)
        if not available:
            return {"available": False, "reason": reason}
        found = conn.execute(
            """
            SELECT o.*, i.source_file_name, i.source_sha256, i.imported_at_utc,
                   i.import_status, i.source_record_count
            FROM retailer_product_observations o
            LEFT JOIN retailer_catalog_imports i ON i.import_id=o.import_id
            WHERE o.observation_id=?
            """,
            (observation_id,),
        ).fetchone()
        if found is None:
            return None
        rows = _attach_related(conn, [dict(found)])
        result = rows[0]
        result["raw_record"] = _decode_json(result.pop("raw_record_json"))
        return result


def list_imports(database: str | Path, *, limit: int = 50) -> dict[str, Any]:
    """List immutable source import metadata in reverse import order."""
    try:
        limit = min(200, max(1, int(limit)))
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer") from None
    with closing(open_readonly(database)) as conn:
        available, reason = _schema_state(conn)
        if not available:
            return {"available": False, "reason": reason, "items": []}
        rows = conn.execute(
            """
            SELECT import_id, retailer_key, source_file_name, source_sha256,
                   source_size_bytes, source_mtime_utc, imported_at_utc,
                   source_record_count, committed_record_count, import_status
            FROM retailer_catalog_imports
            ORDER BY import_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return {"available": True, "items": [dict(row) for row in rows]}
