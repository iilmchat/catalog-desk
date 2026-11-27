"""Streaming storage adapter for the S24 canonical-product JSONL contract.

This module writes only to a caller-selected SQLite connection. It never locates,
opens, or modifies the application's production database by itself.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .schema_v1 import finalize_import, record_observation, register_import, utc_now

CANONICAL_CONTRACT = "catalog-desk.canonical-product"
CANONICAL_SCHEMA_VERSION = 1


def _parse_scraped_at(raw: Any, raw_record: dict[str, Any]) -> tuple[str | None, str]:
    if "scraped_at" not in raw_record:
        return None, "missing"
    if raw is None or raw == "":
        return None, "not_provided"
    if not isinstance(raw, str):
        return None, "invalid"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="microseconds"), "valid"
    except ValueError:
        return None, "invalid"


def canonical_to_storage_dto(document: dict[str, Any]) -> dict[str, Any]:
    """Map S24.2 canonical JSON to the S24.3 storage API without semantic guessing."""
    if not isinstance(document, dict) or document.get("contract") != CANONICAL_CONTRACT:
        raise ValueError("Unsupported canonical document contract")
    if document.get("schema_version") != CANONICAL_SCHEMA_VERSION:
        raise ValueError(f"Unsupported canonical schema version: {document.get('schema_version')!r}")

    retailer = document.get("retailer")
    identity = document.get("identity") or {}
    product = document.get("product") or {}
    links = document.get("links") or {}
    categories = document.get("categories") or {}
    commercial = document.get("commercial") or {}
    observation = document.get("observation") or {}
    raw = document.get("source_record_raw")
    if not isinstance(raw, dict):
        raise ValueError("source_record_raw must be an object")

    if retailer not in {"auchan", "globus", "dixy", "chizhik", "perekrestok", "5ka"}:
        raise ValueError(f"Unsupported retailer key: {retailer!r}")
    if identity.get("status") not in {"resolved", "pending", "invalid"}:
        raise ValueError("Invalid identity status")
    if raw.get("retailer") != document.get("source_retailer_value"):
        raise ValueError("Embedded retailer label does not match source raw record")
    raw_id_present = "product_id" in raw
    raw_id = raw.get("product_id")
    if bool(identity.get("source_id_present")) != raw_id_present:
        raise ValueError("Identity source_id_present does not match raw product_id field presence")
    if identity.get("source_id_raw") != raw_id:
        raise ValueError("Identity source_id_raw does not match raw product_id value")
    external_id = identity.get("external_id")
    identity_status = identity.get("status")
    if identity_status == "resolved":
        if not raw_id_present or raw_id is None or isinstance(raw_id, bool) or str(raw_id) != str(external_id):
            raise ValueError("Resolved external ID does not exactly match raw product_id")
    elif external_id is not None:
        raise ValueError("Pending/invalid identity cannot contain a resolved external ID")
    elif identity_status == "pending" and raw_id_present and raw_id not in (None, ""):
        raise ValueError("Pending identity contains a non-empty raw product_id")

    price_fields: dict[str, Any] = {}
    semantic_roles: dict[str, str] = {}
    canonical_prices = document.get("price_fields") or {}
    if not isinstance(canonical_prices, dict):
        raise ValueError("price_fields must be an object")
    for field_name, field_state in canonical_prices.items():
        if not isinstance(field_state, dict):
            raise ValueError(f"price field {field_name!r} must be an object")
        source_has_field = field_name in raw
        field_state_name = field_state.get("state")
        if field_state_name == "missing":
            if source_has_field:
                raise ValueError(f"Canonical price field {field_name!r} says missing but exists in raw record")
            continue
        if not source_has_field or field_state.get("raw_value") != raw.get(field_name):
            raise ValueError(f"Canonical price field {field_name!r} differs from raw source record")
        price_fields[str(field_name)] = field_state.get("raw_value")
        semantic_roles[str(field_name)] = str(field_state.get("semantic_role") or "unclassified")

    # discount_percent is a monetary-adjacent source field omitted from S24.2's
    # fixed seven-field extraction map; capture it separately when it exists.
    if "discount_percent" in raw and "discount_percent" not in price_fields:
        price_fields["discount_percent"] = raw["discount_percent"]
        semantic_roles["discount_percent"] = "unclassified_source_field"

    category_observations: list[dict[str, Any]] = []
    source_path = categories.get("source_path")
    if source_path != raw.get("category_path"):
        raise ValueError("Canonical category_path differs from raw source record")
    if categories.get("source_slug") != raw.get("category_slug"):
        raise ValueError("Canonical category_slug differs from raw source record")
    if categories.get("source_id") != raw.get("category_id"):
        raise ValueError("Canonical category_id differs from raw source record")
    if categories.get("source_name") != raw.get("category_name"):
        raise ValueError("Canonical category_name differs from raw source record")
    if isinstance(source_path, list):
        category_observations.extend(
            {"source_field": "category_path", "path_position": pos, "value": label}
            for pos, label in enumerate(source_path)
        )
    elif source_path is not None:
        # Preserve malformed/non-array source category values as a single raw item.
        category_observations.append(
            {"source_field": "category_path", "path_position": 0, "value": source_path}
        )
    if categories.get("source_name") is not None:
        category_observations.append(
            {"source_field": "category_name", "path_position": 0, "value": categories.get("source_name")}
        )

    candidate_raw = identity.get("candidate")
    candidate = None
    if candidate_raw:
        candidate_value = candidate_raw.get("external_id_candidate")
        if candidate_value:
            candidate = {
                "value": str(candidate_value),
                "source": f"{candidate_raw.get('candidate_source') or 'unknown'}:{candidate_raw.get('rule_id') or 'unspecified'}",
            }

    scraped_raw = observation.get("scraped_at_raw")
    scraped_utc, scraped_status = _parse_scraped_at(scraped_raw, raw)
    source_id_field = "product_id"  # the six approved source JSON contracts use product_id

    image_classification = links.get("image_url_classification")
    image_classification = {
        "asset_candidate": "image_candidate",
        "invalid_type": "invalid_type",
        "invalid_url": "invalid_url",
        "listing_page": "listing_page",
        "missing": "missing",
        "unclassified_url": "unclassified_url",
    }.get(image_classification, "unclassified_url")

    return {
        "retailer_key": retailer,
        "identity_status": identity["status"],
        "external_product_id": identity.get("external_id"),
        "external_product_id_source_field": source_id_field,
        "source_retailer_label": document.get("source_retailer_value"),
        "identity_candidate": candidate,
        "product_name": product.get("name"),
        "brand": product.get("brand"),
        "product_url": links.get("product_url_raw"),
        "image_url": links.get("image_url_raw"),
        "image_url_classification": image_classification,
        "asset_classification_method": "s24-adapters/0.1.0-local-heuristic",
        "uom": commercial.get("unit_of_measure_raw"),
        "category_slug": categories.get("source_slug"),
        "category_id": categories.get("source_id"),
        "scraped_at_raw": scraped_raw,
        "scraped_at_utc": scraped_utc,
        "scraped_at_status": scraped_status,
        "raw_record": raw,
        "price_fields": price_fields,
        "price_semantic_roles": semantic_roles,
        "currency_code": commercial.get("currency"),
        "currency_status": "declared" if commercial.get("currency") else "not_declared",
        "category_observations": category_observations,
    }


def _stable_record_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _existing_import(conn: sqlite3.Connection, retailer: str, source_sha256: str):
    return conn.execute(
        "SELECT import_id,source_record_count,committed_record_count,import_status "
        "FROM retailer_catalog_imports WHERE retailer_key=? AND source_sha256=?",
        (retailer, source_sha256),
    ).fetchone()


def ingest_one_source(
    conn: sqlite3.Connection,
    *,
    retailer: str,
    source_file_name: str,
    source_sha256: str,
    expected_count: int,
    jsonl_lines: Iterable[bytes],
    imported_at_utc: str | None = None,
) -> dict[str, Any]:
    """Ingest one normalized JSONL source in the caller's transaction.

    Already-committed same-hash imports are treated as no-op only after their stored
    observation count and import accounting are verified.
    """
    existing = _existing_import(conn, retailer, source_sha256)
    if existing:
        import_id = int(existing["import_id"])
        stored = conn.execute(
            "SELECT COUNT(*) FROM retailer_product_observations WHERE import_id=?", (import_id,)
        ).fetchone()[0]
        if (existing["import_status"] != "committed"
                or existing["source_record_count"] != expected_count
                or existing["committed_record_count"] != expected_count
                or stored != expected_count):
            raise sqlite3.IntegrityError(
                f"Existing import is not a complete committed snapshot: {retailer}/{source_sha256}"
            )
        return {"retailer": retailer, "source_records": expected_count,
                "inserted_observations": 0, "skipped_existing_import": True,
                "import_id": import_id}

    import_id, created = register_import(
        conn, retailer_key=retailer, source_file_name=source_file_name,
        source_sha256=source_sha256, source_record_count=expected_count,
        imported_at_utc=imported_at_utc,
    )
    if not created:
        raise sqlite3.IntegrityError("Import appeared concurrently; refusing ambiguous retry")

    count = 0
    resolved = pending = invalid = candidates = 0
    seen_indices: set[int] | None = None
    expected_index = 0
    for line in jsonl_lines:
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSONL record in {source_file_name} at line {count + 1}: {exc}") from exc
        if doc.get("retailer") != retailer:
            raise ValueError(f"Retailer mismatch in {source_file_name} line {count + 1}")
        metadata = doc.get("observation") or {}
        if metadata.get("source_file") != source_file_name:
            raise ValueError(f"Source filename mismatch in {source_file_name} line {count + 1}")
        if metadata.get("source_file_sha256") != source_sha256:
            raise ValueError(f"Source SHA mismatch in {source_file_name} line {count + 1}")
        if metadata.get("source_record_index") != expected_index:
            raise ValueError(
                f"Non-contiguous source_record_index in {source_file_name}: "
                f"expected {expected_index}, got {metadata.get('source_record_index')!r}"
            )
        raw_record = doc.get("source_record_raw")
        if not isinstance(raw_record, dict):
            raise ValueError(f"Missing raw source object in {source_file_name} line {count + 1}")
        if metadata.get("source_record_sha256") != _stable_record_hash(raw_record):
            raise ValueError(f"Raw source record hash mismatch in {source_file_name} index {expected_index}")

        dto = canonical_to_storage_dto(doc)
        record_observation(
            conn, import_id=import_id, source_record_index=expected_index,
            canonical=dto,
        )
        status = dto["identity_status"]
        if status == "resolved": resolved += 1
        elif status == "pending": pending += 1
        else: invalid += 1
        if dto.get("identity_candidate"): candidates += 1
        count += 1
        expected_index += 1

    if count != expected_count:
        raise sqlite3.IntegrityError(
            f"Source record count mismatch for {retailer}: manifest={expected_count}, JSONL={count}"
        )
    finalize_import(conn, import_id, count)
    return {
        "retailer": retailer, "source_records": count,
        "inserted_observations": count, "resolved_identity_records": resolved,
        "pending_identity_records": pending, "invalid_identity_records": invalid,
        "identity_candidates": candidates, "skipped_existing_import": False,
        "import_id": import_id,
    }


def database_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = (
        "retailer_catalog_imports", "retailer_catalog_products", "retailer_product_observations",
        "retailer_product_identity_candidates", "retailer_identity_resolution_events",
        "retailer_product_price_observations", "retailer_product_category_observations",
        "retailer_product_asset_observations",
    )
    result = {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) for name in tables}
    result["imports_by_retailer"] = [dict(row) for row in conn.execute(
        "SELECT retailer_key,source_record_count,committed_record_count,import_status "
        "FROM retailer_catalog_imports ORDER BY retailer_key"
    )]
    result["observations_by_retailer"] = [dict(row) for row in conn.execute(
        "SELECT retailer_key,identity_status,COUNT(*) AS records "
        "FROM retailer_product_observations GROUP BY retailer_key,identity_status "
        "ORDER BY retailer_key,identity_status"
    )]
    result["prices_by_field"] = [dict(row) for row in conn.execute(
        "SELECT field_name,value_state,COUNT(*) AS records FROM retailer_product_price_observations "
        "GROUP BY field_name,value_state ORDER BY field_name,value_state"
    )]
    result["assets_by_classification"] = [dict(row) for row in conn.execute(
        "SELECT url_classification,COUNT(*) AS records FROM retailer_product_asset_observations "
        "GROUP BY url_classification ORDER BY url_classification"
    )]
    return result


def ingest_one_source_bulk(
    conn: sqlite3.Connection,
    *,
    retailer: str,
    source_file_name: str,
    source_sha256: str,
    expected_count: int,
    jsonl_lines: Iterable[bytes],
    batch_size: int = 2000,
    imported_at_utc: str | None = None,
) -> dict[str, Any]:
    """High-throughput equivalent of ingest_one_source using bounded executemany batches."""
    from .schema_v1 import _json, _price_state, _raw_type

    if batch_size < 1 or batch_size > 20000:
        raise ValueError("batch_size must be between 1 and 20000")
    existing = _existing_import(conn, retailer, source_sha256)
    if existing:
        import_id = int(existing["import_id"])
        stored = conn.execute(
            "SELECT COUNT(*) FROM retailer_product_observations WHERE import_id=?", (import_id,)
        ).fetchone()[0]
        if (existing["import_status"] != "committed"
                or existing["source_record_count"] != expected_count
                or existing["committed_record_count"] != expected_count
                or stored != expected_count):
            raise sqlite3.IntegrityError(
                f"Existing import is not a complete committed snapshot: {retailer}/{source_sha256}"
            )
        return {"retailer": retailer, "source_records": expected_count,
                "inserted_observations": 0, "skipped_existing_import": True,
                "import_id": import_id}

    import_id, created = register_import(
        conn, retailer_key=retailer, source_file_name=source_file_name,
        source_sha256=source_sha256, source_record_count=expected_count,
        imported_at_utc=imported_at_utc,
    )
    if not created:
        raise sqlite3.IntegrityError("Import appeared concurrently; refusing ambiguous retry")

    ts = imported_at_utc or utc_now()
    observation_base = int(conn.execute(
        "SELECT COALESCE(MAX(observation_id),0) FROM retailer_product_observations"
    ).fetchone()[0])
    total = resolved = pending = invalid = candidates = 0
    product_rows: list[tuple] = []
    observation_rows: list[tuple] = []
    candidate_rows: list[tuple] = []
    price_rows: list[tuple] = []
    category_rows: list[tuple] = []
    asset_rows: list[tuple] = []
    resolved_ids: set[str] = set()
    last_index = -1

    def flush_batch() -> None:
        if not observation_rows:
            return
        conn.executemany(
            "INSERT INTO retailer_catalog_products "
            "(retailer_key,external_product_id,first_external_id_type,first_seen_at_utc,last_seen_at_utc) "
            "VALUES(?,?,?,?,?) ON CONFLICT(retailer_key,external_product_id) DO UPDATE "
            "SET last_seen_at_utc=COALESCE(excluded.last_seen_at_utc,retailer_catalog_products.last_seen_at_utc)",
            product_rows,
        )
        batch_external_ids = sorted({row[7] for row in observation_rows if row[6] == "resolved" and row[7] is not None})
        product_id_map: dict[str, int] = {}
        if batch_external_ids:
            placeholders = ",".join("?" for _ in batch_external_ids)
            product_id_map = {
                str(r["external_product_id"]): int(r["catalog_product_id"])
                for r in conn.execute(
                    f"SELECT catalog_product_id,external_product_id FROM retailer_catalog_products "
                    f"WHERE retailer_key=? AND external_product_id IN ({placeholders})",
                    (retailer, *batch_external_ids),
                ).fetchall()
            }
        # catalog_product_id is the sixth value in the observation tuple.
        prepared_observation_rows = [
            tuple((*row[:5], product_id_map.get(str(row[7])) if row[6] == "resolved" else None, *row[6:]))
            for row in observation_rows
        ]
        conn.executemany(
            "INSERT INTO retailer_product_observations "
            "(observation_id,import_id,source_record_index,retailer_key,source_retailer_label,catalog_product_id,identity_status,"
            "source_external_product_id,source_external_id_raw_json,source_external_id_type,source_external_id_field,"
            "product_name,brand,product_url,image_url,uom,category_slug,category_id,scraped_at_raw,scraped_at_utc,"
            "scraped_at_status,raw_record_json,created_at_utc) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            prepared_observation_rows,
        )
        if candidate_rows:
            # Candidate values already checked against all resolved IDs in this file and prior committed imports.
            conn.executemany(
                "INSERT INTO retailer_product_identity_candidates "
                "(observation_id,candidate_value,candidate_source,candidate_status,created_at_utc) VALUES(?,?,?,?,?)",
                candidate_rows,
            )
        if price_rows:
            conn.executemany(
                "INSERT INTO retailer_product_price_observations "
                "(observation_id,field_name,raw_value_json,value_state,parsed_decimal_text,parse_status,value_quality,semantic_role,currency_code,currency_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                price_rows,
            )
        if category_rows:
            conn.executemany(
                "INSERT INTO retailer_product_category_observations "
                "(observation_id,source_field,path_position,raw_label_json,label_text) VALUES(?,?,?,?,?)",
                category_rows,
            )
        conn.executemany(
            "INSERT INTO retailer_product_asset_observations "
            "(observation_id,source_field,asset_url,url_classification,classification_method,created_at_utc) "
            "VALUES(?,?,?,?,?,?)",
            asset_rows,
        )
        product_rows.clear()
        observation_rows.clear()
        candidate_rows.clear()
        price_rows.clear()
        category_rows.clear()
        asset_rows.clear()

    # Product IDs can be reused across batches; INSERT..ON CONFLICT is retailer-scoped.
    for line in jsonl_lines:
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSONL record in {source_file_name} at line {total + 1}: {exc}") from exc
        if doc.get("retailer") != retailer:
            raise ValueError(f"Retailer mismatch in {source_file_name} line {total + 1}")
        meta = doc.get("observation") or {}
        if meta.get("source_file") != source_file_name or meta.get("source_file_sha256") != source_sha256:
            raise ValueError(f"Source metadata mismatch in {source_file_name} line {total + 1}")
        idx = meta.get("source_record_index")
        if idx != total:
            raise ValueError(f"Non-contiguous source_record_index in {source_file_name}: expected {total}, got {idx!r}")
        raw = doc.get("source_record_raw")
        if not isinstance(raw, dict) or meta.get("source_record_sha256") != _stable_record_hash(raw):
            raise ValueError(f"Raw source record hash mismatch in {source_file_name} index {idx}")
        dto = canonical_to_storage_dto(doc)
        obs_id = observation_base + total + 1
        status = dto["identity_status"]
        ext_id = dto.get("external_product_id")
        cat_product_id = None
        raw_id_present = "product_id" in raw
        raw_id = raw.get("product_id")
        if raw_id_present:
            if raw_id is None:
                id_type = "null"
            elif isinstance(raw_id, str) and not raw_id.strip():
                id_type = "empty"
            else:
                id_type = _raw_type(raw_id)
        else:
            id_type = "missing"
        if id_type not in {"missing", "null", "empty", "string", "integer", "number", "boolean", "invalid"}:
            id_type = "invalid"

        if status == "resolved":
            if not ext_id:
                raise ValueError("Resolved identity has no external ID")
            product_rows.append((retailer, ext_id, _raw_type(raw_id), dto.get("scraped_at_utc"), dto.get("scraped_at_utc")))
            resolved_ids.add(ext_id)
            resolved += 1
        elif status == "pending":
            if ext_id is not None:
                raise ValueError("Pending identity must not carry a resolved external ID")
            pending += 1
        else:
            if ext_id is not None:
                raise ValueError("Invalid identity must not carry a resolved external ID")
            invalid += 1

        observation_rows.append((
            obs_id, import_id, total, retailer, dto.get("source_retailer_label"),
            None, status, ext_id, _json(raw_id) if raw_id_present else None, id_type,
            "product_id", dto.get("product_name"), dto.get("brand"), dto.get("product_url"),
            dto.get("image_url"), dto.get("uom"), dto.get("category_slug"),
            str(dto.get("category_id")) if dto.get("category_id") is not None else None,
            dto.get("scraped_at_raw"), dto.get("scraped_at_utc"), dto.get("scraped_at_status", "not_provided"),
            _json(raw), ts,
        ))

        candidate = dto.get("identity_candidate")
        if candidate:
            candidate_value = str(candidate["value"])
            # Resolve collision status against already-existing product identities;
            # candidates in later records will be checked against IDs from the same batch at flush time.
            exists = conn.execute(
                "SELECT 1 FROM retailer_catalog_products WHERE retailer_key=? AND external_product_id=?",
                (retailer, candidate_value),
            ).fetchone() is not None
            candidate_status = "collision" if exists or candidate_value in resolved_ids else "unverified"
            candidate_rows.append((obs_id, candidate_value, str(candidate["source"]), candidate_status, ts))
            candidates += 1

        currency_code = dto.get("currency_code")
        currency_status = dto.get("currency_status", "not_declared")
        if currency_status not in {"declared", "not_declared", "invalid"}:
            currency_status = "invalid"
        for field_name, value in dto.get("price_fields", {}).items():
            state, parse_status, parsed, quality = _price_state(value)
            price_rows.append((obs_id, str(field_name), _json(value), state, parsed, parse_status, quality,
                               str(dto.get("price_semantic_roles", {}).get(field_name, "unclassified")),
                               currency_code, currency_status))

        for item in dto.get("category_observations", []):
            label = item["value"]
            category_rows.append((obs_id, str(item.get("source_field") or "category_path"),
                                  int(item.get("path_position", 0)), _json(label), label if isinstance(label, str) else None))

        asset_class = dto.get("image_url_classification") or classify_asset_url(dto.get("image_url"))
        allowed = {"image_candidate", "listing_page", "invalid_url", "missing", "unclassified_url", "invalid_type"}
        if asset_class not in allowed:
            asset_class = "unclassified_url"
        asset_rows.append((obs_id, "image_url", dto.get("image_url"), asset_class,
                           dto.get("asset_classification_method", "s24_adapter_heuristic"), ts))

        total += 1
        last_index = idx
        if len(observation_rows) >= batch_size:
            flush_batch()
    flush_batch()

    if total != expected_count:
        raise sqlite3.IntegrityError(
            f"Source record count mismatch for {retailer}: manifest={expected_count}, JSONL={total}"
        )
    # Resolve candidate collisions against the complete retailer ID set, including IDs appearing later in the file.
    conn.execute(
        "UPDATE retailer_product_identity_candidates AS c SET candidate_status='collision' "
        "WHERE candidate_status='unverified' AND EXISTS ("
        "SELECT 1 FROM retailer_product_observations AS o "
        "JOIN retailer_catalog_products AS p ON p.retailer_key=o.retailer_key "
        "AND p.external_product_id=c.candidate_value WHERE o.observation_id=c.observation_id)"
    )
    # Product IDs are maintained with a unique constraint; count all confirmed identities in this retailer.
    stored_product_count = conn.execute(
        "SELECT COUNT(*) FROM retailer_catalog_products WHERE retailer_key=?", (retailer,)
    ).fetchone()[0]
    finalize_import(conn, import_id, total)
    return {
        "retailer": retailer, "source_records": total,
        "inserted_observations": total, "resolved_identity_records": resolved,
        "pending_identity_records": pending, "invalid_identity_records": invalid,
        "identity_candidates": candidates, "confirmed_products_in_db": stored_product_count,
        "skipped_existing_import": False, "import_id": import_id,
    }
