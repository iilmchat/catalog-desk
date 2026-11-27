#!/usr/bin/env python3
"""Run a Dixy-only S24.5 pilot against a disposable copy of an existing S24 DB.

The source application database is opened read-only and copied using SQLite's
Backup API. All import writes go exclusively to a new disposable DB beneath
--out-dir. Missing Dixy product IDs remain pending; URL-derived IDs are stored
only as unverified candidates and are never promoted automatically.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from s24.schema_v1 import PRICE_FIELDS, classify_asset_url, connect  # noqa: E402
from s24.storage_v1 import database_counts, ingest_one_source_bulk  # noqa: E402

RETAILER = "dixy"
# Pinned to the most recent source snapshot supplied by the S24 sample extractor.
EXPECTED_SOURCE_SHA256 = "8295a61ee1da54ff27638dac2c0d592f1eef59a2caa8362b964af12e1d765777"
EXPECTED_SOURCE_RECORDS = 15311
CANDIDATE_RULE_ID = "dixy-terminal-url-id-v1"
CANDIDATE_RE = re.compile(r"-(\d{10})/?$")
S24_TABLES = (
    "retailer_catalog_imports",
    "retailer_catalog_products",
    "retailer_product_observations",
    "retailer_product_identity_candidates",
    "retailer_identity_resolution_events",
    "retailer_product_price_observations",
    "retailer_product_category_observations",
    "retailer_product_asset_observations",
)
REQUIRED_S24_TABLES = set(S24_TABLES) | {"retailer_catalog_meta"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_file_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "sha256": sha256_file(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def db_sidecar_state(path: Path) -> dict[str, str | None]:
    # The WAL contains durable committed pages not yet checkpointed into the main
    # DB file. SHM is transient lock/index memory and is intentionally not hashed.
    result: dict[str, str | None] = {"database_sha256": sha256_file(path)}
    wal = Path(str(path) + "-wal")
    result["wal_sha256"] = sha256_file(wal) if wal.exists() else None
    return result


def iter_json_array(path: Path, chunk_size: int = 1024 * 1024) -> Iterator[dict[str, Any]]:
    """Incrementally parse a top-level JSON array without loading the full catalog."""
    decoder = json.JSONDecoder()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        buffer = ""
        pos = 0
        eof = False
        started = False
        expect_value = True
        after_comma = False
        finished = False

        def refill() -> bool:
            nonlocal buffer, eof
            if eof:
                return False
            chunk = stream.read(chunk_size)
            if chunk == "":
                eof = True
                return False
            buffer += chunk
            return True

        while not finished:
            while True:
                while pos < len(buffer) and buffer[pos].isspace():
                    pos += 1
                if pos < len(buffer) or eof:
                    break
                refill()

            if not started:
                if pos >= len(buffer) or buffer[pos] != "[":
                    raise ValueError("Dixy source root must be a JSON array")
                pos += 1
                started = True
                continue

            if expect_value:
                if pos < len(buffer) and buffer[pos] == "]":
                    if after_comma:
                        raise ValueError("Trailing comma in source JSON array")
                    pos += 1
                    finished = True
                    break
                if pos >= len(buffer) and eof:
                    raise ValueError("Unexpected EOF inside source JSON array")
                try:
                    value, end = decoder.raw_decode(buffer, pos)
                except json.JSONDecodeError:
                    if refill():
                        continue
                    raise
                if not isinstance(value, dict):
                    raise ValueError(f"Source array item must be an object, got {type(value).__name__}")
                yield value
                pos = end
                expect_value = False
                after_comma = False
            else:
                if pos < len(buffer) and buffer[pos] == ",":
                    pos += 1
                    expect_value = True
                    after_comma = True
                elif pos < len(buffer) and buffer[pos] == "]":
                    pos += 1
                    finished = True
                elif pos >= len(buffer) and eof:
                    raise ValueError("Unexpected EOF after source JSON array item")
                else:
                    if pos >= len(buffer):
                        refill()
                    else:
                        raise ValueError(f"Expected comma or closing bracket at character {pos}")

            if pos > 2 * chunk_size:
                buffer = buffer[pos:]
                pos = 0

        tail = buffer[pos:]
        while True:
            if tail.strip():
                raise ValueError("Unexpected content after source JSON array")
            if eof:
                break
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            tail += chunk


def stable_record_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(
        record, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dixy_url_id_candidate(url: Any) -> str | None:
    """Extract a conservative terminal 10-digit candidate from dixy.ru product URLs.

    product_key is deliberately not used. A candidate is only metadata for review.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in {"http", "https"} or host not in {"dixy.ru", "www.dixy.ru"}:
        return None
    if not parsed.path.startswith("/product/"):
        return None
    match = CANDIDATE_RE.search(parsed.path)
    return match.group(1) if match else None


def _source_id_kind(record: dict[str, Any]) -> str:
    if "product_id" not in record:
        return "missing"
    value = record.get("product_id")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "empty" if not value.strip() else "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "invalid"


def validate_dixy_source(
    path: Path,
    expected_sha256: str | None = EXPECTED_SOURCE_SHA256,
    expected_count: int | None = EXPECTED_SOURCE_RECORDS,
) -> dict[str, Any]:
    before = source_file_state(path)
    if expected_sha256 and before["sha256"] != expected_sha256.lower():
        raise ValueError("Dixy source SHA-256 differs from the approved source snapshot")

    resolved_ids: set[str] = set()
    resolved_id_types: Counter[str] = Counter()
    identity_kinds: Counter[str] = Counter()
    candidates: list[str] = []
    candidate_by_record: Counter[str] = Counter()
    price_field_counts = {field: 0 for field in PRICE_FIELDS}
    category_observation_count = 0
    image_classifications: Counter[str] = Counter()
    record_count = 0
    sample_resolved_ids: list[str] = []
    sample_candidates: list[str] = []

    for index, record in enumerate(iter_json_array(path)):
        if record.get("retailer") != RETAILER:
            raise ValueError(f"Unexpected retailer at source index {index}: {record.get('retailer')!r}")
        id_kind = _source_id_kind(record)
        identity_kinds[id_kind] += 1
        raw_id = record.get("product_id")
        if id_kind in {"boolean", "invalid", "number"}:
            raise ValueError(f"Unsupported Dixy product_id type at source index {index}: {type(raw_id).__name__}")
        if id_kind in {"string", "integer"}:
            external_id = str(raw_id)
            if not external_id.strip():
                raise ValueError(f"Empty Dixy product_id at source index {index}")
            if external_id in resolved_ids:
                raise ValueError(f"Duplicate Dixy product_id {external_id!r} at source index {index}")
            resolved_ids.add(external_id)
            resolved_id_types["string" if isinstance(raw_id, str) else "integer"] += 1
            if len(sample_resolved_ids) < 5:
                sample_resolved_ids.append(external_id)
        else:
            candidate = dixy_url_id_candidate(record.get("url"))
            if candidate:
                candidates.append(candidate)
                candidate_by_record[candidate] += 1
                if len(sample_candidates) < 5:
                    sample_candidates.append(candidate)

        for field in PRICE_FIELDS:
            if field in record:
                price_field_counts[field] += 1

        # Dixy currently exports category_name (not category_path) in the sampled file.
        path_value = record.get("category_path")
        if isinstance(path_value, list):
            category_observation_count += len(path_value)
        elif path_value is not None:
            category_observation_count += 1
        if record.get("category_name") is not None:
            category_observation_count += 1

        image_classifications[classify_asset_url(record.get("image_url"))] += 1
        stable_record_hash(record)
        record_count += 1

    after = source_file_state(path)
    if before != after:
        raise RuntimeError("Dixy source file changed during preflight")
    if expected_count is not None and record_count != expected_count:
        raise ValueError(f"Expected {expected_count} records, found {record_count}")

    candidate_counts = Counter(candidates)
    duplicate_candidate_groups = sum(1 for n in candidate_counts.values() if n > 1)
    known_collisions = sorted(set(candidates) & resolved_ids)
    pending_records = identity_kinds["missing"] + identity_kinds["null"] + identity_kinds["empty"]

    return {
        "source_file": path.name,
        "source_path": str(path.resolve()),
        "source_sha256": before["sha256"],
        "source_size_bytes": before["size_bytes"],
        "source_mtime_ns": before["mtime_ns"],
        "source_mtime_utc": before["mtime_utc"],
        "source_record_count": record_count,
        "resolved_identity_records": len(resolved_ids),
        "pending_identity_records": pending_records,
        "invalid_identity_records": identity_kinds["boolean"] + identity_kinds["invalid"] + identity_kinds["number"],
        "identity_source_state_counts": dict(sorted(identity_kinds.items())),
        "resolved_external_id_types": dict(sorted(resolved_id_types.items())),
        "unique_resolved_external_ids": len(resolved_ids),
        "duplicate_resolved_external_ids": 0,
        "url_identity_candidate_records": len(candidates),
        "unique_url_identity_candidates": len(candidate_counts),
        "duplicate_url_identity_candidate_groups": duplicate_candidate_groups,
        "candidate_known_id_collisions": len(known_collisions),
        "candidate_collision_values_sample": known_collisions[:10],
        "candidate_source_rule": CANDIDATE_RULE_ID,
        "url_candidates_sample": sample_candidates,
        "resolved_ids_sample_preserved_as_strings": sample_resolved_ids,
        "price_field_presence_counts": price_field_counts,
        "expected_price_observation_count": sum(price_field_counts.values()),
        "expected_category_observation_count": category_observation_count,
        "expected_asset_observation_count": record_count,
        "image_url_classifications": dict(sorted(image_classifications.items())),
    }


def canonical_dixy_document(raw: dict[str, Any], index: int,
                             source_file_name: str, source_sha256: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("retailer") != RETAILER:
        raise ValueError(f"Unexpected retailer in canonical Dixy record at source index {index}")

    id_kind = _source_id_kind(raw)
    if id_kind in {"boolean", "invalid", "number"}:
        raise ValueError(f"Invalid Dixy product_id type at source index {index}")
    raw_id = raw.get("product_id")
    resolved = id_kind in {"string", "integer"}
    if resolved and not str(raw_id).strip():
        raise ValueError(f"Empty Dixy product_id at source index {index}")

    candidate_value = None if resolved else dixy_url_id_candidate(raw.get("url"))
    candidate = None
    if candidate_value:
        candidate = {
            "external_id_candidate": candidate_value,
            "candidate_source": "url",
            "rule_id": CANDIDATE_RULE_ID,
            "verified": False,
            "raw_url": raw.get("url"),
            "known_product_id_collision": None,
        }

    price_fields: dict[str, Any] = {}
    for field in PRICE_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if value is None:
            state = "null"
        elif isinstance(value, str) and not value.strip():
            state = "empty"
        else:
            state = "value"
        price_fields[field] = {
            "state": state,
            "raw_value": value,
            "semantic_role": "unclassified_source_field",
        }

    category_path = raw.get("category_path")
    category_name = raw.get("category_name")
    image_class = classify_asset_url(raw.get("image_url"))
    canonical_image_class = "asset_candidate" if image_class == "image_candidate" else image_class

    return {
        "contract": "catalog-desk.canonical-product",
        "schema_version": 1,
        "retailer": RETAILER,
        "source_retailer_value": raw.get("retailer"),
        "identity": {
            "status": "resolved" if resolved else "pending",
            "external_id": str(raw_id) if resolved else None,
            "source_id_present": "product_id" in raw,
            "source_id_raw": raw_id,
            "candidate": candidate,
        },
        "product": {
            "name": raw.get("name"),
            "brand": raw.get("brand"),
        },
        "links": {
            "product_url_raw": raw.get("url"),
            "image_url_raw": raw.get("image_url"),
            "image_url_classification": canonical_image_class,
        },
        "categories": {
            "source_path": category_path,
            "source_slug": raw.get("category_slug"),
            "source_id": raw.get("category_id"),
            "source_name": category_name,
        },
        "commercial": {
            "unit_of_measure_raw": raw.get("uom"),
            "currency": None,
        },
        "price_fields": price_fields,
        "observation": {
            "source_file": source_file_name,
            "source_file_sha256": source_sha256,
            "source_record_index": index,
            "source_record_sha256": stable_record_hash(raw),
            "scraped_at_raw": raw.get("scraped_at"),
        },
        "source_record_raw": raw,
    }


def canonical_lines(path: Path, source_file_name: str, source_sha256: str) -> Iterator[bytes]:
    for index, raw in enumerate(iter_json_array(path)):
        doc = canonical_dixy_document(raw, index, source_file_name, source_sha256)
        yield (json.dumps(doc, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def legacy_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
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


def legacy_fingerprint_sha256(fingerprint: dict[str, Any]) -> str:
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def legacy_table_row_counts(fingerprint: dict[str, Any]) -> dict[str, int]:
    return {obj["name"]: obj["row_count"] for obj in fingerprint["objects"] if obj["type"] == "table"}


def s24_table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in S24_TABLES}


def copy_database_read_only(source_db: Path, destination_db: Path) -> dict[str, Any]:
    if destination_db.exists():
        raise FileExistsError(f"Refusing to overwrite disposable DB: {destination_db}")
    source = sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    target = sqlite3.connect(destination_db, timeout=30)
    try:
        source.backup(target, pages=512, sleep=0.01)
        target.row_factory = sqlite3.Row
        quick = target.execute("PRAGMA quick_check").fetchone()[0]
        fk = target.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or fk:
            raise RuntimeError(f"Backup validation failed: quick_check={quick}, FK={len(fk)}")
        present = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_S24_TABLES - present
        if missing:
            raise RuntimeError(f"Source DB is missing S24 tables: {sorted(missing)}")
        schema_version = target.execute(
            "SELECT meta_value FROM retailer_catalog_meta WHERE meta_key='schema_version'").fetchone()
        if schema_version is None or schema_version[0] != "1":
            raise RuntimeError("S24 schema_version is not 1")
        initial_counts = s24_table_counts(target)
        if any(initial_counts.values()):
            raise RuntimeError(f"Expected empty S24 data tables before pilot, found: {initial_counts}")
        legacy = legacy_fingerprint(target)
        journal_mode = target.execute("PRAGMA journal_mode").fetchone()[0]
        target.commit()
        return {
            "quick_check": quick,
            "foreign_key_violations": len(fk),
            "s24_schema_version": schema_version[0],
            "s24_initial_data_counts": initial_counts,
            "legacy_object_count": len(legacy["objects"]),
            "legacy_fingerprint": legacy,
            "source_journal_mode": journal_mode,
        }
    finally:
        target.close()
        source.close()


def run_pilot(source_db: Path, source_json: Path, out_dir: Path,
              expected_source_sha256: str | None = EXPECTED_SOURCE_SHA256,
              expected_source_count: int | None = EXPECTED_SOURCE_RECORDS) -> dict[str, Any]:
    source_db = source_db.resolve()
    source_json = source_json.resolve()
    out_dir = out_dir.resolve()
    if not source_db.is_file() or not source_json.is_file():
        raise FileNotFoundError(source_db if not source_db.is_file() else source_json)
    if out_dir.exists():
        raise FileExistsError(f"Refusing to reuse existing output directory: {out_dir}")
    if os.path.normcase(str(source_db)) == os.path.normcase(str(out_dir / "dixy_pilot.sqlite3")):
        raise ValueError("Disposable DB path must differ from source DB")

    started = time.monotonic()
    # Complete source validation before creating any output or database copy.
    source_preflight = validate_dixy_source(source_json, expected_source_sha256, expected_source_count)
    db_before = db_sidecar_state(source_db)
    out_dir.mkdir(parents=True)
    db_out = out_dir / "dixy_pilot.sqlite3"
    report_out = out_dir / "dixy_pilot_report.json"
    db_snapshot = copy_database_read_only(source_db, db_out)

    conn = connect(str(db_out))
    try:
        before_legacy = db_snapshot["legacy_fingerprint"]
        conn.execute("BEGIN IMMEDIATE")
        try:
            first_result = ingest_one_source_bulk(
                conn,
                retailer=RETAILER,
                source_file_name=source_json.name,
                source_sha256=source_preflight["source_sha256"],
                expected_count=source_preflight["source_record_count"],
                jsonl_lines=canonical_lines(source_json, source_json.name, source_preflight["source_sha256"]),
                batch_size=2000,
            )
            conn.execute(
                "UPDATE retailer_catalog_imports SET source_size_bytes=?,source_mtime_utc=? "
                "WHERE retailer_key=? AND source_sha256=?",
                (source_preflight["source_size_bytes"], source_preflight["source_mtime_utc"],
                 RETAILER, source_preflight["source_sha256"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        conn.execute("BEGIN IMMEDIATE")
        try:
            repeat_result = ingest_one_source_bulk(
                conn,
                retailer=RETAILER,
                source_file_name=source_json.name,
                source_sha256=source_preflight["source_sha256"],
                expected_count=source_preflight["source_record_count"],
                jsonl_lines=iter(()),
                batch_size=2000,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        counts = database_counts(conn)
        dixy_products = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_catalog_products WHERE retailer_key='dixy'").fetchone()[0])
        dixy_observations = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_observations WHERE retailer_key='dixy'").fetchone()[0])
        identity_rows = [dict(row) for row in conn.execute(
            "SELECT identity_status,COUNT(*) AS records FROM retailer_product_observations "
            "WHERE retailer_key='dixy' GROUP BY identity_status ORDER BY identity_status").fetchall()]
        identity_source_types = [dict(row) for row in conn.execute(
            "SELECT source_external_id_type,COUNT(*) AS records FROM retailer_product_observations "
            "WHERE retailer_key='dixy' GROUP BY source_external_id_type ORDER BY source_external_id_type").fetchall()]
        dixy_prices = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_price_observations p "
            "JOIN retailer_product_observations o USING(observation_id) WHERE o.retailer_key='dixy'").fetchone()[0])
        dixy_categories = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_category_observations c "
            "JOIN retailer_product_observations o USING(observation_id) WHERE o.retailer_key='dixy'").fetchone()[0])
        dixy_assets = int(conn.execute(
            "SELECT COUNT(*) FROM retailer_product_asset_observations a "
            "JOIN retailer_product_observations o USING(observation_id) WHERE o.retailer_key='dixy'").fetchone()[0])
        candidate_rows = [dict(row) for row in conn.execute(
            "SELECT candidate_status,COUNT(*) AS records,COUNT(DISTINCT candidate_value) AS unique_values "
            "FROM retailer_product_identity_candidates c JOIN retailer_product_observations o USING(observation_id) "
            "WHERE o.retailer_key='dixy' GROUP BY candidate_status ORDER BY candidate_status").fetchall()]
        price_by_field = [dict(row) for row in conn.execute(
            "SELECT p.field_name,p.value_state,COUNT(*) AS records "
            "FROM retailer_product_price_observations p JOIN retailer_product_observations o USING(observation_id) "
            "WHERE o.retailer_key='dixy' GROUP BY p.field_name,p.value_state ORDER BY p.field_name,p.value_state").fetchall()]
        categories_by_source_field = [dict(row) for row in conn.execute(
            "SELECT c.source_field,COUNT(*) AS records FROM retailer_product_category_observations c "
            "JOIN retailer_product_observations o USING(observation_id) WHERE o.retailer_key='dixy' "
            "GROUP BY c.source_field ORDER BY c.source_field").fetchall()]
        assets_by_class = [dict(row) for row in conn.execute(
            "SELECT a.url_classification,COUNT(*) AS records FROM retailer_product_asset_observations a "
            "JOIN retailer_product_observations o USING(observation_id) WHERE o.retailer_key='dixy' "
            "GROUP BY a.url_classification ORDER BY a.url_classification").fetchall()]

        after_legacy = legacy_fingerprint(conn)
        after_db_source = db_sidecar_state(source_db)
        source_after = source_file_state(source_json)
        if source_after != {"sha256": source_preflight["source_sha256"],
                            "size_bytes": source_preflight["source_size_bytes"],
                            "mtime_ns": source_preflight["source_mtime_ns"],
                            "mtime_utc": source_preflight["source_mtime_utc"]}:
            raise RuntimeError("Dixy source file changed during the pilot")
        if after_db_source != db_before:
            raise RuntimeError("Source application database or WAL/SHM changed during the pilot")
        if after_legacy != before_legacy:
            raise AssertionError("Legacy schema/objects/row counts changed in disposable DB")
        if first_result.get("inserted_observations") != source_preflight["source_record_count"]:
            raise AssertionError("First import did not insert the full Dixy record count")
        if repeat_result.get("skipped_existing_import") is not True or repeat_result.get("inserted_observations") != 0:
            raise AssertionError("Repeat import was not an idempotent no-op")
        if dixy_observations != source_preflight["source_record_count"]:
            raise AssertionError("Dixy observation count differs from source")
        if dixy_products != source_preflight["resolved_identity_records"]:
            raise AssertionError("Dixy confirmed product count differs from resolved identities")
        if dixy_prices != source_preflight["expected_price_observation_count"]:
            raise AssertionError("Dixy price observation count mismatch")
        if dixy_categories != source_preflight["expected_category_observation_count"]:
            raise AssertionError("Dixy category observation count mismatch")
        if dixy_assets != source_preflight["expected_asset_observation_count"]:
            raise AssertionError("Dixy asset observation count mismatch")
        if sum(r["records"] for r in identity_rows) != source_preflight["source_record_count"]:
            raise AssertionError("Dixy identity status counts do not reconcile")
        candidate_total = sum(r["records"] for r in candidate_rows)
        if candidate_total != source_preflight["url_identity_candidate_records"]:
            raise AssertionError("Dixy candidate rows differ from the URL-derived candidate preflight")
        if any(row["candidate_status"] not in {"unverified", "collision"} for row in candidate_rows):
            raise AssertionError("Unexpected candidate status; candidate may have been promoted/rejected")

        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if quick != "ok" or fk:
            raise AssertionError(f"Post-load SQLite check failed: {quick}, FK={len(fk)}")

        report = {
            "contract": "catalog-desk.s24-dixy-pilot-report",
            "schema_version": 1,
            "mode": "disposable_copy_pilot_only",
            "production_database_modified": False,
            "production_database_accessed_read_only": True,
            "source_json_modified": False,
            "source_db": str(source_db),
            "source_db_fingerprint_before_after": [db_before, after_db_source],
            "source_snapshot_db": str(db_out),
            "source_db_snapshot": {k: v for k, v in db_snapshot.items() if k != "legacy_fingerprint"},
            "source_file": source_preflight,
            "source_file_unchanged_after_pilot": True,
            "retailer": RETAILER,
            "first_import": first_result,
            "repeat_import": repeat_result,
            "dixy_counts": {
                "observations": dixy_observations,
                "confirmed_products": dixy_products,
                "expected_confirmed_products": source_preflight["resolved_identity_records"],
                "identity_status": identity_rows,
                "identity_source_types": identity_source_types,
                "pending_records": source_preflight["pending_identity_records"],
                "invalid_records": source_preflight["invalid_identity_records"],
                "url_identity_candidate_records": source_preflight["url_identity_candidate_records"],
                "unique_url_identity_candidates": source_preflight["unique_url_identity_candidates"],
                "duplicate_url_identity_candidate_groups": source_preflight["duplicate_url_identity_candidate_groups"],
                "candidate_known_id_collisions_preflight": source_preflight["candidate_known_id_collisions"],
                "candidate_status": candidate_rows,
                "price_observations": dixy_prices,
                "expected_price_observations": source_preflight["expected_price_observation_count"],
                "price_by_field_and_state": price_by_field,
                "category_observations": dixy_categories,
                "expected_category_observations": source_preflight["expected_category_observation_count"],
                "category_by_source_field": categories_by_source_field,
                "asset_observations": dixy_assets,
                "expected_asset_observations": source_preflight["expected_asset_observation_count"],
                "assets_by_classification": assets_by_class,
            },
            "database_counts": counts,
            "legacy_objects_and_row_counts_unchanged": True,
            "legacy_object_count": len(before_legacy["objects"]),
            "legacy_fingerprint_sha256_before_after": [legacy_fingerprint_sha256(before_legacy), legacy_fingerprint_sha256(after_legacy)],
            "legacy_table_row_counts": legacy_table_row_counts(after_legacy),
            "sqlite_quick_check": quick,
            "foreign_key_violations": len(fk),
            "idempotency_noop_confirmed": True,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        conn.close()
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(db_out) + suffix)
            if candidate.exists():
                candidate.unlink()
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--source-json", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path, help="New disposable output directory; must not exist")
    parser.add_argument("--expected-source-sha256", default=EXPECTED_SOURCE_SHA256)
    parser.add_argument("--expected-source-count", type=int, default=EXPECTED_SOURCE_RECORDS)
    args = parser.parse_args()
    try:
        report = run_pilot(args.source_db, args.source_json, args.out_dir,
                           args.expected_source_sha256, args.expected_source_count)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    c = report["dixy_counts"]
    print("Retailer: dixy")
    print("Source records:", report["source_file"]["source_record_count"])
    print("Confirmed products:", c["confirmed_products"])
    print("Pending observations:", c["pending_records"])
    print("URL identity candidates:", c["url_identity_candidate_records"])
    print("Unique URL candidates:", c["unique_url_identity_candidates"])
    print("Candidate known-ID collisions:", c["candidate_known_id_collisions_preflight"])
    print("Observations:", c["observations"])
    print("Price observations:", c["price_observations"])
    print("Category observations:", c["category_observations"])
    print("Asset observations:", c["asset_observations"])
    print("Repeat import no-op:", report["idempotency_noop_confirmed"])
    print("Legacy unchanged:", report["legacy_objects_and_row_counts_unchanged"])
    print("Integrity:", report["sqlite_quick_check"], "FK:", report["foreign_key_violations"])
    print("Elapsed seconds:", report["elapsed_seconds"])
    print("DISPOSABLE_DB=" + str(args.out_dir / "dixy_pilot.sqlite3"))
    print("REPORT=" + str(args.out_dir / "dixy_pilot_report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
