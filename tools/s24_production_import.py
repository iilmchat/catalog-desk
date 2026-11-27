#!/usr/bin/env python3
"""Guarded, atomic S24.5 production import for the six approved retailer snapshots.

Preflight creates and validates a separate SQLite Backup-API backup. Apply requires
that preflight report and backup, revalidates every source, then imports all six
sources in one BEGIN IMMEDIATE transaction. Any exception before commit rolls back
all six imports. No schema migration, legacy-table update, queue operation, network
request, or photo download is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from s24.storage_v1 import database_counts, ingest_one_source_bulk  # noqa: E402

S24_DATA_TABLES = (
    "retailer_catalog_imports",
    "retailer_catalog_products",
    "retailer_product_observations",
    "retailer_product_identity_candidates",
    "retailer_identity_resolution_events",
    "retailer_product_price_observations",
    "retailer_product_category_observations",
    "retailer_product_asset_observations",
)
REQUIRED_TABLES = set(S24_DATA_TABLES) | {"retailer_catalog_meta"}

SOURCES = (
    {
        "slug": "auchan", "retailer_key": "auchan", "file": "auchan_products.json",
        "module": "tools.s24_auchan_pilot", "validator": "validate_auchan_source",
    },
    {
        "slug": "globus", "retailer_key": "globus", "file": "globus_products.json",
        "module": "tools.s24_globus_pilot", "validator": "validate_globus_source",
    },
    {
        "slug": "dixy", "retailer_key": "dixy", "file": "dixy_products.json",
        "module": "tools.s24_dixy_pilot", "validator": "validate_dixy_source",
    },
    {
        "slug": "chizhik", "retailer_key": "chizhik", "file": "chizhik_products.json",
        "module": "tools.s24_chizhik_pilot", "validator": "validate_chizhik_source",
    },
    {
        "slug": "perekrestok", "retailer_key": "perekrestok", "file": "perekrestok_products.json",
        "module": "tools.s24_perekrestok_pilot", "validator": "validate_perekrestok_source",
    },
    {
        "slug": "pyaterochka", "retailer_key": "5ka", "file": "pyaterochka_products.json",
        "module": "tools.s24_pyaterochka_pilot", "validator": "validate_pyaterochka_source",
    },
)

# Reviewed, source-specific outcomes. These guard against a mapper or manifest drift.
EXPECTED_SOURCE_OUTCOMES = {
    "auchan": {"records": 81413, "resolved": 81413, "pending": 0, "invalid": 0, "candidates": 0,
                "prices": 407065, "categories": 238372, "assets": 81413},
    "globus": {"records": 48158, "resolved": 48158, "pending": 0, "invalid": 0, "candidates": 0,
                "prices": 288948, "categories": 67453, "assets": 48158},
    "dixy": {"records": 15311, "resolved": 8427, "pending": 6884, "invalid": 0, "candidates": 6593,
              "prices": 45900, "categories": 15278, "assets": 15311},
    "chizhik": {"records": 3231, "resolved": 3231, "pending": 0, "invalid": 0, "candidates": 0,
                 "prices": 6462, "categories": 3657, "assets": 3231},
    "perekrestok": {"records": 6002, "resolved": 6002, "pending": 0, "invalid": 0, "candidates": 0,
                    "prices": 8022, "categories": 0, "assets": 6002},
    "pyaterochka": {"records": 5643, "resolved": 5643, "pending": 0, "invalid": 0, "candidates": 0,
                    "prices": 11286, "categories": 0, "assets": 5643},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def maybe_sha256(path: Path) -> str | None:
    return sha256_file(path) if path.exists() else None


def db_file_state(db_path: Path) -> dict[str, Any]:
    wal = Path(str(db_path) + "-wal")
    return {
        "database_sha256": maybe_sha256(db_path),
        "database_size_bytes": db_path.stat().st_size if db_path.exists() else None,
        "database_mtime_ns": db_path.stat().st_mtime_ns if db_path.exists() else None,
        "wal_sha256": maybe_sha256(wal),
        "wal_size_bytes": wal.stat().st_size if wal.exists() else None,
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _legacy_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE type IN ('table','index','trigger','view') "
        "AND name NOT LIKE 'sqlite_%' AND tbl_name NOT LIKE 'retailer_%' "
        "ORDER BY type,name"
    ).fetchall()
    objects = []
    for row in rows:
        item = {"type": row["type"], "name": row["name"], "tbl_name": row["tbl_name"], "sql": row["sql"]}
        if row["type"] == "table":
            quoted = '"' + row["name"].replace('"', '""') + '"'
            item["row_count"] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        objects.append(item)
    return {"objects": objects}


def _fingerprint_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _s24_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in S24_DATA_TABLES}


def _database_state(conn: sqlite3.Connection) -> dict[str, Any]:
    missing = REQUIRED_TABLES - _table_names(conn)
    if missing:
        raise RuntimeError(f"Рабочая SQLite не содержит все S24-таблицы: {sorted(missing)}")
    quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if quick != "ok" or fk:
        raise RuntimeError(f"SQLite integrity check failed: quick_check={quick}, FK={len(fk)}")
    version_row = conn.execute(
        "SELECT meta_value FROM retailer_catalog_meta WHERE meta_key='schema_version'"
    ).fetchone()
    if version_row is None or str(version_row[0]) != "1":
        raise RuntimeError("Ожидалась S24 schema_version=1; миграцию в этом сценарии не выполняем")
    legacy = _legacy_fingerprint(conn)
    return {
        "quick_check": quick,
        "foreign_key_violations": len(fk),
        "s24_schema_version": str(version_row[0]),
        "s24_data_counts": _s24_counts(conn),
        "legacy_object_count": len(legacy["objects"]),
        "legacy_fingerprint_sha256": _fingerprint_sha256(legacy),
        "legacy_table_row_counts": {
            obj["name"]: obj["row_count"] for obj in legacy["objects"] if obj["type"] == "table"
        },
        "journal_mode": str(conn.execute("PRAGMA journal_mode").fetchone()[0]),
        "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
    }


def load_source_modules() -> list[dict[str, Any]]:
    loaded = []
    for spec in SOURCES:
        module = importlib.import_module(spec["module"])
        if module.RETAILER != spec["retailer_key"]:
            raise RuntimeError(f"Mapper retailer mismatch for {spec['slug']}")
        spec2 = dict(spec)
        spec2["module_obj"] = module
        spec2["expected_sha256"] = module.EXPECTED_SOURCE_SHA256
        spec2["expected_count"] = module.EXPECTED_SOURCE_RECORDS
        if not hasattr(module, spec["validator"]) or not hasattr(module, "canonical_lines"):
            raise RuntimeError(f"Incomplete mapper module for {spec['slug']}")
        loaded.append(spec2)
    return loaded


def validate_sources(source_dir: Path) -> list[dict[str, Any]]:
    profiles = []
    for i, spec in enumerate(load_source_modules(), 1):
        path = source_dir / spec["file"]
        if not path.is_file():
            raise FileNotFoundError(f"Не найден источник {spec['slug']}: {path}")
        module = spec["module_obj"]
        validator = getattr(module, spec["validator"])
        kwargs = {"expected_sha256": spec["expected_sha256"], "expected_count": spec["expected_count"]}
        if spec["slug"] in {"chizhik", "perekrestok", "pyaterochka"}:
            kwargs["enforce_reviewed_profile"] = True
        profile = validator(path, **kwargs)
        expected = EXPECTED_SOURCE_OUTCOMES[spec["slug"]]
        observed = {
            "records": profile["source_record_count"],
            "resolved": profile.get("resolved_identity_records", profile.get("unique_external_ids")),
            "pending": profile.get("pending_identity_records", 0),
            "invalid": profile.get("invalid_identity_records", 0),
            "candidates": profile.get("url_identity_candidate_records", 0),
            "prices": profile["expected_price_observation_count"],
            "categories": profile["expected_category_observation_count"],
            "assets": profile["expected_asset_observation_count"],
        }
        if observed != expected:
            raise RuntimeError(
                f"Профиль {spec['slug']} не совпал с проверенным S24.5 snapshot: "
                f"expected={expected}, observed={observed}"
            )
        if profile["source_sha256"] != spec["expected_sha256"]:
            raise RuntimeError(f"SHA256 mismatch for {spec['slug']}")
        if spec["slug"] == "dixy":
            dixy_checks = {
                "unique_url_identity_candidates": 6593,
                "duplicate_url_identity_candidate_groups": 0,
                "candidate_known_id_collisions": 0,
            }
            for field, expected_value in dixy_checks.items():
                if profile.get(field) != expected_value:
                    raise RuntimeError(f"Dixy candidate profile mismatch: {field}={profile.get(field)!r}, expected={expected_value}")
        print(f"[{i}/6] {spec['slug']}: {profile['source_record_count']:,} rows; SHA256 OK")
        profiles.append({
            "slug": spec["slug"],
            "retailer_key": spec["retailer_key"],
            "file": spec["file"],
            "module": spec["module"],
            "validator": spec["validator"],
            "expected_sha256": spec["expected_sha256"],
            "expected_count": spec["expected_count"],
            "profile": profile,
        })
    return profiles


def _validate_profile_recheck(source_dir: Path, preflight_profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = validate_sources(source_dir)
    by_slug = {p["slug"]: p for p in preflight_profiles}
    for item in current:
        previous = by_slug.get(item["slug"])
        if previous is None:
            raise RuntimeError(f"Source {item['slug']} отсутствует в preflight report")
        if (item["profile"]["source_sha256"] != previous["profile"]["source_sha256"]
                or item["profile"]["source_record_count"] != previous["profile"]["source_record_count"]):
            raise RuntimeError(f"Source {item['slug']} изменился после preflight")
    return current


def create_backup(source_db: Path, backup_path: Path, expected_state: dict[str, Any]) -> dict[str, Any]:
    if backup_path.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {backup_path}")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source = _connect_readonly(source_db)
    target = sqlite3.connect(str(backup_path), timeout=30)
    try:
        source.backup(target, pages=512, sleep=0.01)
        target.commit()
    finally:
        target.close()
        source.close()
    backup_conn = _connect_readonly(backup_path)
    try:
        backup_state = _database_state(backup_conn)
    finally:
        backup_conn.close()
    for key in ("legacy_fingerprint_sha256", "s24_schema_version", "s24_data_counts", "user_version"):
        if backup_state[key] != expected_state[key]:
            raise RuntimeError(f"Backup logical verification mismatch for {key}")
    return {
        "path": str(backup_path.resolve()),
        "sha256": sha256_file(backup_path),
        "size_bytes": backup_path.stat().st_size,
        "quick_check": backup_state["quick_check"],
        "foreign_key_violations": backup_state["foreign_key_violations"],
        "legacy_fingerprint_sha256": backup_state["legacy_fingerprint_sha256"],
        "s24_data_counts": backup_state["s24_data_counts"],
    }


def run_preflight(database: Path, source_dir: Path, backup_dir: Path, report_out: Path) -> dict[str, Any]:
    database = database.resolve()
    source_dir = source_dir.resolve()
    backup_dir = backup_dir.resolve()
    report_out = report_out.resolve()
    if not database.is_file():
        raise FileNotFoundError(f"Working DB not found: {database}")
    if report_out.exists():
        raise FileExistsError(f"Refusing to overwrite report: {report_out}")

    print("=== SOURCE PREFLIGHT ===")
    profiles = validate_sources(source_dir)
    total_source_bytes = sum(int(x["profile"]["source_size_bytes"]) for x in profiles)
    backup_dir.mkdir(parents=True, exist_ok=True)
    free_bytes = shutil.disk_usage(backup_dir).free
    required_free = max(1024**3, total_source_bytes * 4)
    if free_bytes < required_free:
        raise RuntimeError(
            f"Insufficient free disk space on backup volume: {free_bytes:,}; "
            f"required at least {required_free:,} bytes"
        )

    print("\n=== WORKING DB READ-ONLY CHECK ===")
    db_before = db_file_state(database)
    conn = _connect_readonly(database)
    try:
        state = _database_state(conn)
    finally:
        conn.close()
    if state["legacy_object_count"] != 24:
        raise RuntimeError(
            f"Ожидалось 24 legacy schema objects из проверенного состояния проекта, "
            f"получено {state['legacy_object_count']}; сначала разберём расхождение."
        )
    if state["journal_mode"].lower() != "wal" or state["user_version"] != 0:
        raise RuntimeError(
            f"Неожиданные SQLite settings: journal_mode={state['journal_mode']}, "
            f"user_version={state['user_version']}"
        )
    if any(state["s24_data_counts"].values()):
        raise RuntimeError(
            "Рабочие S24 data tables уже не пустые. Автоматически смешивать данные с пилотами нельзя. "
            f"Current counts: {state['s24_data_counts']}"
        )
    db_after_read = db_file_state(database)
    if (db_before["database_sha256"] != db_after_read["database_sha256"]
            or db_before["wal_sha256"] != db_after_read["wal_sha256"]):
        raise RuntimeError("Рабочая SQLite/WAL менялась во время read-only проверки; повторите preflight при стабильной БД")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"pyaterochka_catalog_v9_before_s24_5_production_import_{stamp}.sqlite3"
    print("\n=== CREATE SQLITE BACKUP ===")
    backup = create_backup(database, backup_path, state)
    db_after_backup = db_file_state(database)
    if (db_before["database_sha256"] != db_after_backup["database_sha256"]
            or db_before["wal_sha256"] != db_after_backup["wal_sha256"]):
        raise RuntimeError("Рабочая SQLite/WAL изменилась во время Backup API; backup сохранён, import не запускался")
    if state["s24_schema_version"] != "1":
        raise RuntimeError("Unexpected S24 schema version")

    report = {
        "contract": "catalog-desk.s24-production-import-preflight",
        "schema_version": 1,
        "mode": "preflight_backup_only_no_production_writes",
        "created_at_utc": utc_now(),
        "production_database": str(database),
        "source_directory": str(source_dir),
        "source_profiles": profiles,
        "total_source_records": sum(int(x["profile"]["source_record_count"]) for x in profiles),
        "reviewed_expected_totals": {
            "confirmed_products": sum(x["resolved"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
            "observations": sum(x["records"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
            "pending_observations": sum(x["pending"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
            "identity_candidates": sum(x["candidates"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
            "price_observations": sum(x["prices"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
            "category_observations": sum(x["categories"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
            "asset_observations": sum(x["assets"] for x in EXPECTED_SOURCE_OUTCOMES.values()),
        },
        "database_state_before": state,
        "database_files_before": db_before,
        "database_files_after_backup": db_after_backup,
        "backup": backup,
        "production_database_modified": False,
        "preflight_passed": True,
    }
    report_out.parent.mkdir(parents=True, exist_ok=True)
    tmp_report = report_out.with_suffix(report_out.suffix + ".tmp")
    tmp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_report, report_out)
    print(f"\nPREFLIGHT_REPORT={report_out}")
    print(f"BACKUP={backup_path}")
    print(f"BACKUP_SHA256={backup['sha256']}")
    print("Production DB writes: NONE")
    return report


def _query_counts(conn: sqlite3.Connection, source_specs: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> dict[str, Any]:
    by_slug = {p["slug"]: p["profile"] for p in profiles}
    per_retailer: dict[str, Any] = {}
    for spec in source_specs:
        slug = spec["slug"]
        retailer = spec["retailer_key"]
        profile = by_slug[slug]
        product_count = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_catalog_products WHERE retailer_key=?", (retailer,)
        ).fetchone()[0])
        status_rows = conn.execute(
            "SELECT identity_status,COUNT(*) AS n FROM retailer_product_observations "
            "WHERE retailer_key=? GROUP BY identity_status", (retailer,)
        ).fetchall()
        statuses = {str(r["identity_status"]): int(r["n"]) for r in status_rows}
        observation_count = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_observations WHERE retailer_key=?", (retailer,)
        ).fetchone()[0])
        price_rows = conn.execute(
            "SELECT p.field_name,COUNT(*) AS n FROM retailer_product_price_observations p "
            "JOIN retailer_product_observations o ON o.observation_id=p.observation_id "
            "WHERE o.retailer_key=? GROUP BY p.field_name", (retailer,)
        ).fetchall()
        price_by_field = {str(r["field_name"]): int(r["n"]) for r in price_rows}
        category_count = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_category_observations c "
            "JOIN retailer_product_observations o ON o.observation_id=c.observation_id "
            "WHERE o.retailer_key=?", (retailer,)
        ).fetchone()[0])
        asset_rows = conn.execute(
            "SELECT a.url_classification,COUNT(*) AS n FROM retailer_product_asset_observations a "
            "JOIN retailer_product_observations o ON o.observation_id=a.observation_id "
            "WHERE o.retailer_key=? GROUP BY a.url_classification", (retailer,)
        ).fetchall()
        asset_by_class = {str(r["url_classification"]): int(r["n"]) for r in asset_rows}
        candidate_rows = conn.execute(
            "SELECT c.candidate_status,COUNT(*) AS n FROM retailer_product_identity_candidates c "
            "JOIN retailer_product_observations o ON o.observation_id=c.observation_id "
            "WHERE o.retailer_key=? GROUP BY c.candidate_status", (retailer,)
        ).fetchall()
        candidate_by_status = {str(r["candidate_status"]): int(r["n"]) for r in candidate_rows}

        expected_resolved = int(profile.get("resolved_identity_records", profile.get("unique_external_ids", 0)))
        expected_pending = int(profile.get("pending_identity_records", 0))
        expected_invalid = int(profile.get("invalid_identity_records", 0))
        expected_prices = {k: int(v) for k, v in profile["price_field_presence_counts"].items() if int(v) > 0}
        expected_assets = {k: int(v) for k, v in profile["image_url_classifications"].items() if int(v) > 0}
        expected_candidates = int(profile.get("url_identity_candidate_records", 0))
        expected_label = "pyaterochka" if retailer == "5ka" else retailer
        unexpected_labels = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_observations WHERE retailer_key=? AND "
            "(source_retailer_label IS NULL OR source_retailer_label!=?)", (retailer, expected_label)
        ).fetchone()[0])
        stored_slug_count = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_observations WHERE retailer_key=? AND category_slug IS NOT NULL",
            (retailer,),
        ).fetchone()[0])
        expected_slug_count = profile.get("category_slug_count")
        actual_price_states = {
            f"{r['field_name']}:{r['value_state']}": int(r["n"])
            for r in conn.execute(
                "SELECT p.field_name,p.value_state,COUNT(*) AS n "
                "FROM retailer_product_price_observations p "
                "JOIN retailer_product_observations o ON o.observation_id=p.observation_id "
                "WHERE o.retailer_key=? GROUP BY p.field_name,p.value_state", (retailer,)
            ).fetchall()
        }
        expected_price_states = profile.get("price_field_state_counts")
        if expected_price_states is not None:
            expected_price_states = {str(k): int(v) for k, v in expected_price_states.items() if int(v) > 0}

        expected_collision_candidates = int(profile.get("candidate_known_id_collisions", 0))
        expected_candidate_statuses = {}
        if expected_candidates - expected_collision_candidates:
            expected_candidate_statuses["unverified"] = expected_candidates - expected_collision_candidates
        if expected_collision_candidates:
            expected_candidate_statuses["collision"] = expected_collision_candidates

        checks = {
            "products": (product_count, expected_resolved),
            "observations": (observation_count, int(profile["source_record_count"])),
            "resolved": (statuses.get("resolved", 0), expected_resolved),
            "pending": (statuses.get("pending", 0), expected_pending),
            "invalid": (statuses.get("invalid", 0), expected_invalid),
            "prices": (sum(price_by_field.values()), int(profile["expected_price_observation_count"])),
            "price_fields": (price_by_field, expected_prices),
            "categories": (category_count, int(profile["expected_category_observation_count"])),
            "assets": (sum(asset_by_class.values()), int(profile["expected_asset_observation_count"])),
            "asset_classifications": (asset_by_class, expected_assets),
            "candidates": (sum(candidate_by_status.values()), expected_candidates),
            "candidate_statuses": (candidate_by_status, expected_candidate_statuses),
            "unexpected_source_labels": (unexpected_labels, 0),
        }
        if expected_price_states is not None:
            checks["price_states"] = (actual_price_states, expected_price_states)
        if expected_slug_count is not None:
            checks["category_slug_metadata"] = (stored_slug_count, int(expected_slug_count))
        failures = {k: {"actual": a, "expected": e} for k, (a, e) in checks.items() if a != e}
        if failures:
            raise RuntimeError(f"Stored S24 counts do not reconcile for {slug}: {failures}")
        if candidate_by_status.get("promoted", 0):
            raise RuntimeError(f"Unexpected promoted identity candidates for {slug}")
        per_retailer[retailer] = {
            "source_records": int(profile["source_record_count"]),
            "confirmed_products": product_count,
            "observations": observation_count,
            "identity_status": statuses,
            "price_by_field": price_by_field,
            "price_observations": sum(price_by_field.values()),
            "category_observations": category_count,
            "asset_by_classification": asset_by_class,
            "asset_observations": sum(asset_by_class.values()),
            "identity_candidates_by_status": candidate_by_status,
            "identity_candidates": sum(candidate_by_status.values()),
            "checks": {k: "pass" for k in checks},
        }

    import_rows = conn.execute(
        "SELECT retailer_key,source_file_name,source_sha256,source_record_count,committed_record_count,import_status "
        "FROM retailer_catalog_imports ORDER BY retailer_key"
    ).fetchall()
    expected_imports = {
        (s["retailer_key"], s["file"], s["expected_sha256"], s["expected_count"])
        for s in source_specs
    }
    actual_imports = {
        (str(r["retailer_key"]), str(r["source_file_name"]), str(r["source_sha256"]), int(r["source_record_count"]))
        for r in import_rows
        if r["import_status"] == "committed" and int(r["committed_record_count"]) == int(r["source_record_count"])
    }
    if len(import_rows) != 6 or actual_imports != expected_imports:
        raise RuntimeError("retailer_catalog_imports does not contain exactly the six expected committed snapshots")

    counts = database_counts(conn)
    expected_totals = {
        "retailer_catalog_imports": 6,
        "retailer_catalog_products": sum(EXPECTED_SOURCE_OUTCOMES[s["slug"]]["resolved"] for s in source_specs),
        "retailer_product_observations": sum(EXPECTED_SOURCE_OUTCOMES[s["slug"]]["records"] for s in source_specs),
        "retailer_product_identity_candidates": sum(EXPECTED_SOURCE_OUTCOMES[s["slug"]]["candidates"] for s in source_specs),
        "retailer_identity_resolution_events": 0,
        "retailer_product_price_observations": sum(EXPECTED_SOURCE_OUTCOMES[s["slug"]]["prices"] for s in source_specs),
        "retailer_product_category_observations": sum(EXPECTED_SOURCE_OUTCOMES[s["slug"]]["categories"] for s in source_specs),
        "retailer_product_asset_observations": sum(EXPECTED_SOURCE_OUTCOMES[s["slug"]]["assets"] for s in source_specs),
    }
    for table, expected in expected_totals.items():
        if int(counts[table]) != expected:
            raise RuntimeError(f"Total count mismatch in {table}: actual={counts[table]}, expected={expected}")

    currency_rows = int(conn.execute(
        "SELECT COUNT(*) FROM retailer_product_price_observations "
        "WHERE currency_code IS NOT NULL OR currency_status!='not_declared'"
    ).fetchone()[0])
    if currency_rows:
        raise RuntimeError(f"Unexpected inferred/declared currency on {currency_rows} price observations")
    bad_semantic_rows = int(conn.execute(
        "SELECT COUNT(*) FROM retailer_product_price_observations "
        "WHERE semantic_role NOT IN ('unclassified','unclassified_source_field')"
    ).fetchone()[0])
    if bad_semantic_rows:
        raise RuntimeError(f"Unexpected inferred semantic role on {bad_semantic_rows} price observations")
    verified_assets = int(conn.execute(
        "SELECT COUNT(*) FROM retailer_product_asset_observations WHERE verification_status!='unverified'"
    ).fetchone()[0])
    if verified_assets:
        raise RuntimeError(f"Unexpected verified assets found: {verified_assets}")
    return {"totals": counts, "expected_totals": expected_totals, "per_retailer": per_retailer,
            "currency_or_semantic_inference_rows": currency_rows,
            "verified_asset_rows": verified_assets}


def run_apply(database: Path, source_dir: Path, preflight_report: Path, report_out: Path) -> dict[str, Any]:
    database = database.resolve()
    source_dir = source_dir.resolve()
    preflight_report = preflight_report.resolve()
    report_out = report_out.resolve()
    if not preflight_report.is_file():
        raise FileNotFoundError(f"Preflight report not found: {preflight_report}")
    if report_out.exists():
        raise FileExistsError(f"Refusing to overwrite final report: {report_out}")

    preflight = json.loads(preflight_report.read_text(encoding="utf-8"))
    if preflight.get("contract") != "catalog-desk.s24-production-import-preflight" or not preflight.get("preflight_passed"):
        raise RuntimeError("Invalid or failed preflight report")
    if Path(preflight["production_database"]).resolve() != database:
        raise RuntimeError("Preflight report belongs to a different production database")
    backup = preflight.get("backup") or {}
    backup_path = Path(backup.get("path", ""))
    if not backup_path.is_file() or sha256_file(backup_path) != backup.get("sha256"):
        raise RuntimeError("Verified pre-import backup is missing or its SHA-256 changed")

    print("=== REVALIDATE SIX SOURCE SNAPSHOTS ===")
    source_specs = load_source_modules()
    profiles = _validate_profile_recheck(source_dir, preflight["source_profiles"])
    profile_by_slug = {x["slug"]: x["profile"] for x in profiles}

    backup_conn = _connect_readonly(backup_path)
    try:
        backup_state = _database_state(backup_conn)
    finally:
        backup_conn.close()
    before_state_expected = preflight["database_state_before"]
    for key in ("legacy_fingerprint_sha256", "s24_schema_version", "s24_data_counts", "user_version"):
        if backup_state[key] != before_state_expected[key]:
            raise RuntimeError(f"Backup no longer matches preflight logical state: {key}")

    total_source_bytes = sum(int(x["profile"]["source_size_bytes"]) for x in profiles)
    free_bytes = shutil.disk_usage(database.parent).free
    required_free = max(1024**3, total_source_bytes * 3)
    if free_bytes < required_free:
        raise RuntimeError(
            f"Insufficient free disk space on working DB volume: {free_bytes:,}; "
            f"required at least {required_free:,} bytes"
        )

    conn = sqlite3.connect(str(database), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        conn.close()
        raise RuntimeError("Could not enable SQLite foreign key enforcement")

    start = time.monotonic()
    source_results = []
    committed = False
    database_modified = False
    try:
        print("\n=== BEGIN ATOMIC PRODUCTION TRANSACTION ===")
        conn.execute("BEGIN IMMEDIATE")
        state_at_lock = _database_state(conn)
        for key in ("legacy_fingerprint_sha256", "s24_schema_version", "user_version"):
            if state_at_lock[key] != before_state_expected[key]:
                raise RuntimeError(f"Working DB changed after preflight: {key}")

        # The only allowed pre-existing S24 state is either completely empty or a
        # fully committed copy of exactly these six source snapshots (idempotent rerun).
        counts_at_lock = state_at_lock["s24_data_counts"]
        is_empty = not any(counts_at_lock.values())
        print(f"S24 data tables initially empty: {is_empty}")

        for i, spec in enumerate(source_specs, 1):
            profile = profile_by_slug[spec["slug"]]
            module = spec["module_obj"]
            source_path = source_dir / spec["file"]
            print(f"\n[{i}/6] Importing {spec['slug']} ({profile['source_record_count']:,} rows)")
            first = ingest_one_source_bulk(
                conn,
                retailer=spec["retailer_key"],
                source_file_name=spec["file"],
                source_sha256=profile["source_sha256"],
                expected_count=int(profile["source_record_count"]),
                jsonl_lines=module.canonical_lines(source_path, spec["file"], profile["source_sha256"]),
                batch_size=2000,
            )
            repeat = ingest_one_source_bulk(
                conn,
                retailer=spec["retailer_key"],
                source_file_name=spec["file"],
                source_sha256=profile["source_sha256"],
                expected_count=int(profile["source_record_count"]),
                jsonl_lines=(),
                batch_size=2000,
            )
            if not repeat.get("skipped_existing_import") or int(repeat.get("inserted_observations", -1)) != 0:
                raise RuntimeError(f"Idempotency repeat check failed for {spec['slug']}: {repeat}")
            print(
                f"  inserted={first.get('inserted_observations', 0):,}; "
                f"resolved={first.get('resolved_identity_records', 0):,}; "
                f"pending={first.get('pending_identity_records', 0):,}; no-op repeat=PASS"
            )
            database_modified = database_modified or (not bool(first.get("skipped_existing_import")))
            source_results.append({"retailer": spec["retailer_key"], "first_import": first, "repeat_import": repeat})

        print("\n=== VERIFY STORED COUNTS BEFORE COMMIT ===")
        verification = _query_counts(conn, source_specs, profiles)
        for spec in source_specs:
            module = spec["module_obj"]
            source_path = source_dir / spec["file"]
            final_state = module.source_file_state(source_path)
            expected_hash = profile_by_slug[spec["slug"]]["source_sha256"]
            if final_state["sha256"] != expected_hash:
                raise RuntimeError(f"Source JSON changed during production import: {spec['file']}")

        legacy_after = _legacy_fingerprint(conn)
        legacy_after_sha = _fingerprint_sha256(legacy_after)
        if legacy_after_sha != before_state_expected["legacy_fingerprint_sha256"]:
            raise RuntimeError("Legacy schema objects or row counts changed inside the production transaction")
        quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or fk:
            raise RuntimeError(f"Pre-commit integrity check failed: quick_check={quick}, FK={len(fk)}")

        conn.commit()
        committed = True
        print("\nCOMMIT: all six retailer imports committed atomically")
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()

    after_conn = _connect_readonly(database)
    try:
        after_state = _database_state(after_conn)
        after_counts = _query_counts(after_conn, source_specs, profiles)
    finally:
        after_conn.close()

    if after_state["legacy_fingerprint_sha256"] != before_state_expected["legacy_fingerprint_sha256"]:
        raise RuntimeError("Post-commit legacy fingerprint mismatch")
    if after_state["foreign_key_violations"] != 0 or after_state["quick_check"] != "ok":
        raise RuntimeError("Post-commit SQLite integrity check failed")

    report = {
        "contract": "catalog-desk.s24-production-import-report",
        "schema_version": 1,
        "mode": "production_database_import",
        "started_from_preflight": str(preflight_report),
        "committed_at_utc": utc_now(),
        "production_database": str(database),
        "backup": backup,
        "source_profiles": profiles,
        "source_import_results": source_results,
        "verification_before_commit": verification,
        "verification_after_commit": after_counts,
        "database_state_before": before_state_expected,
        "database_state_after": after_state,
        "database_files_after_commit": db_file_state(database),
        "legacy_objects_and_row_counts_unchanged": True,
        "production_database_modified": bool(database_modified),
        "production_database_write_transaction_committed": bool(committed),
        "production_import_committed": bool(committed),
        "production_database_accessed_read_only": False,
        "all_six_sources_committed": len(source_results) == 6,
        "idempotency_noop_confirmed_for_each_source": all(
            x["repeat_import"].get("skipped_existing_import") for x in source_results
        ),
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }
    report_out.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_out.with_suffix(report_out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, report_out)
    print(f"\nFINAL_REPORT={report_out}")
    print(f"DATABASE={database}")
    print(f"Confirmed products: {after_counts['totals']['retailer_catalog_products']:,}")
    print(f"Observations: {after_counts['totals']['retailer_product_observations']:,}")
    print(f"Price observations: {after_counts['totals']['retailer_product_price_observations']:,}")
    print(f"Category observations: {after_counts['totals']['retailer_product_category_observations']:,}")
    print(f"Asset observations: {after_counts['totals']['retailer_product_asset_observations']:,}")
    print(f"Identity candidates: {after_counts['totals']['retailer_product_identity_candidates']:,}")
    print(f"SQLite integrity: {after_state['quick_check']}; FK: {after_state['foreign_key_violations']}")
    print("Legacy unchanged: True")
    print(f"Production DB modified: {report['production_database_modified']}")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Guarded S24.5 production import for six approved retailer snapshots")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="validate sources and create a verified DB backup; no production writes")
    pre.add_argument("--database", required=True, type=Path)
    pre.add_argument("--source-dir", required=True, type=Path)
    pre.add_argument("--backup-dir", required=True, type=Path)
    pre.add_argument("--report-out", required=True, type=Path)

    apply = sub.add_parser("apply", help="atomically import all six sources into the working DB")
    apply.add_argument("--database", required=True, type=Path)
    apply.add_argument("--source-dir", required=True, type=Path)
    apply.add_argument("--preflight-report", required=True, type=Path)
    apply.add_argument("--report-out", required=True, type=Path)
    apply.add_argument("--apply", action="store_true", help="explicitly enable production writes")

    args = parser.parse_args()
    try:
        if args.command == "preflight":
            run_preflight(args.database, args.source_dir, args.backup_dir, args.report_out)
            return 0
        if not args.apply:
            raise RuntimeError("Production write refused: pass --apply explicitly")
        run_apply(args.database, args.source_dir, args.preflight_report, args.report_out)
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
