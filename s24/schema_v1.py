"""S24.3 additive schema prototype. Does not open or locate any database itself."""
from __future__ import annotations

import json
from functools import wraps
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA_VERSION = 1
RETAILERS = ("auchan", "globus", "dixy", "chizhik", "perekrestok", "5ka")
PRICE_FIELDS = (
    "price", "order_price", "old_price", "price_old", "promo_price",
    "price_min", "price_max", "discount_percent",
)
RESOLUTION_EVIDENCE_TYPES = {"manual_review", "source_verified", "official_source"}

DDL = r"""
CREATE TABLE IF NOT EXISTS retailer_catalog_meta (
    meta_key TEXT PRIMARY KEY,
    meta_value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retailer_catalog_imports (
    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer_key TEXT NOT NULL CHECK (retailer_key IN ('auchan','globus','dixy','chizhik','perekrestok','5ka')),
    source_file_name TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
    source_size_bytes INTEGER CHECK (source_size_bytes IS NULL OR source_size_bytes >= 0),
    source_mtime_utc TEXT,
    imported_at_utc TEXT NOT NULL,
    source_record_count INTEGER NOT NULL CHECK (source_record_count >= 0),
    committed_record_count INTEGER NOT NULL DEFAULT 0 CHECK (committed_record_count >= 0),
    import_status TEXT NOT NULL DEFAULT 'staged' CHECK (import_status IN ('staged','committed','failed')),
    UNIQUE (retailer_key, source_sha256)
);

-- Stable identities exist only for source-confirmed IDs. Pending observations do not create products.
CREATE TABLE IF NOT EXISTS retailer_catalog_products (
    catalog_product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    retailer_key TEXT NOT NULL CHECK (retailer_key IN ('auchan','globus','dixy','chizhik','perekrestok','5ka')),
    external_product_id TEXT NOT NULL CHECK (length(external_product_id) > 0),
    first_external_id_type TEXT NOT NULL CHECK (first_external_id_type IN ('string','integer','number','boolean','unknown')),
    first_seen_at_utc TEXT,
    last_seen_at_utc TEXT,
    UNIQUE (retailer_key, external_product_id),
    UNIQUE (catalog_product_id, retailer_key)
);

-- One immutable raw-source observation per row in a source file. Only identity_status/catalog_product_id
-- may change during an explicitly audited pending -> resolved decision; raw_record_json never changes.
CREATE TABLE IF NOT EXISTS retailer_product_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id INTEGER NOT NULL REFERENCES retailer_catalog_imports(import_id) ON DELETE RESTRICT,
    source_record_index INTEGER NOT NULL CHECK (source_record_index >= 0),
    retailer_key TEXT NOT NULL CHECK (retailer_key IN ('auchan','globus','dixy','chizhik','perekrestok','5ka')),
    source_retailer_label TEXT,
    catalog_product_id INTEGER,
    identity_status TEXT NOT NULL CHECK (identity_status IN ('resolved','pending','invalid')),
    source_external_product_id TEXT,
    source_external_id_raw_json TEXT,
    source_external_id_type TEXT NOT NULL CHECK (source_external_id_type IN ('missing','null','empty','string','integer','number','boolean','invalid')),
    source_external_id_field TEXT,
    product_name TEXT,
    brand TEXT,
    product_url TEXT,
    image_url TEXT,
    uom TEXT,
    category_slug TEXT,
    category_id TEXT,
    scraped_at_raw TEXT,
    scraped_at_utc TEXT,
    scraped_at_status TEXT NOT NULL DEFAULT 'not_provided' CHECK (scraped_at_status IN ('valid','missing','invalid','not_provided')),
    raw_record_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (import_id, source_record_index),
    UNIQUE (observation_id, retailer_key),
    FOREIGN KEY (catalog_product_id, retailer_key)
        REFERENCES retailer_catalog_products(catalog_product_id, retailer_key) ON DELETE RESTRICT,
    CHECK (
        (identity_status='resolved' AND catalog_product_id IS NOT NULL) OR
        (identity_status IN ('pending','invalid') AND catalog_product_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_retailer_observations_product_time
    ON retailer_product_observations(catalog_product_id, scraped_at_utc);
CREATE INDEX IF NOT EXISTS idx_retailer_observations_identity
    ON retailer_product_observations(retailer_key, identity_status, source_external_product_id);
CREATE TRIGGER IF NOT EXISTS trg_retailer_observation_source_immutable
BEFORE UPDATE OF import_id,source_record_index,retailer_key,source_retailer_label,
    source_external_product_id,source_external_id_raw_json,source_external_id_type,
    source_external_id_field,product_name,brand,product_url,image_url,uom,category_slug,
    category_id,scraped_at_raw,scraped_at_utc,scraped_at_status,raw_record_json,created_at_utc
ON retailer_product_observations
BEGIN
    SELECT RAISE(ABORT, 'source observation fields are immutable');
END;

CREATE TABLE IF NOT EXISTS retailer_product_identity_candidates (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES retailer_product_observations(observation_id) ON DELETE RESTRICT,
    candidate_value TEXT NOT NULL CHECK (length(candidate_value) > 0),
    candidate_source TEXT NOT NULL,
    candidate_status TEXT NOT NULL DEFAULT 'unverified' CHECK (candidate_status IN ('unverified','collision','rejected','promoted')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (observation_id, candidate_value, candidate_source)
);
CREATE INDEX IF NOT EXISTS idx_retailer_identity_candidate_value
    ON retailer_product_identity_candidates(candidate_value, candidate_status);

CREATE TABLE IF NOT EXISTS retailer_identity_resolution_events (
    resolution_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL,
    retailer_key TEXT NOT NULL,
    catalog_product_id INTEGER NOT NULL,
    resolved_external_product_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN ('manual_review','source_verified','official_source')),
    evidence_reference TEXT NOT NULL CHECK (length(trim(evidence_reference)) > 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    note TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (observation_id, retailer_key)
        REFERENCES retailer_product_observations(observation_id, retailer_key) ON DELETE RESTRICT,
    FOREIGN KEY (catalog_product_id, retailer_key)
        REFERENCES retailer_catalog_products(catalog_product_id, retailer_key) ON DELETE RESTRICT
);

-- One row for each price field actually present in the source object. Absence is represented
-- by no row; JSON null, empty string, invalid text and numeric values remain distinct.
CREATE TABLE IF NOT EXISTS retailer_product_price_observations (
    price_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES retailer_product_observations(observation_id) ON DELETE RESTRICT,
    field_name TEXT NOT NULL,
    raw_value_json TEXT NOT NULL,
    value_state TEXT NOT NULL CHECK (value_state IN ('null','empty','value','invalid')),
    parsed_decimal_text TEXT,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('parsed','not_numeric','not_attempted')),
    value_quality TEXT NOT NULL DEFAULT 'unassessed' CHECK (value_quality IN ('unassessed','positive','zero_or_negative')),
    semantic_role TEXT NOT NULL DEFAULT 'unclassified',
    currency_code TEXT,
    currency_status TEXT NOT NULL DEFAULT 'not_declared' CHECK (currency_status IN ('declared','not_declared','invalid')),
    UNIQUE (observation_id, field_name)
);
CREATE INDEX IF NOT EXISTS idx_retailer_price_observation_field
    ON retailer_product_price_observations(field_name, observation_id);

CREATE TABLE IF NOT EXISTS retailer_product_category_observations (
    category_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES retailer_product_observations(observation_id) ON DELETE RESTRICT,
    source_field TEXT NOT NULL DEFAULT 'category_path',
    path_position INTEGER NOT NULL CHECK (path_position >= 0),
    raw_label_json TEXT NOT NULL,
    label_text TEXT,
    UNIQUE (observation_id, source_field, path_position)
);

CREATE TABLE IF NOT EXISTS retailer_product_asset_observations (
    asset_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES retailer_product_observations(observation_id) ON DELETE RESTRICT,
    source_field TEXT NOT NULL DEFAULT 'image_url',
    asset_url TEXT,
    url_classification TEXT NOT NULL CHECK (url_classification IN ('image_candidate','listing_page','invalid_url','missing','unclassified_url','invalid_type')),
    classification_method TEXT NOT NULL DEFAULT 'local_heuristic',
    verification_status TEXT NOT NULL DEFAULT 'unverified' CHECK (verification_status IN ('unverified','verified_image','verified_not_image')),
    created_at_utc TEXT NOT NULL,
    UNIQUE (observation_id, source_field)
);
"""



def _atomic_savepoint(name: str):
    """Wrap a multi-statement DB operation in a nested savepoint."""
    def decorate(func):
        @wraps(func)
        def wrapped(conn: sqlite3.Connection, *args, **kwargs):
            conn.execute(f"SAVEPOINT {name}")
            try:
                result = func(conn, *args, **kwargs)
                conn.execute(f"RELEASE SAVEPOINT {name}")
                return result
            except Exception:
                try:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    conn.execute(f"RELEASE SAVEPOINT {name}")
                except sqlite3.Error:
                    pass
                raise
        return wrapped
    return decorate


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    """Open a DB connection with FK enforcement enabled; caller owns path selection."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")
    return conn


def apply_schema(conn: sqlite3.Connection) -> bool:
    """Apply additive S24.3 tables in a savepoint. Never changes legacy schema_meta/user_version."""
    current = conn.execute(
        "SELECT meta_value FROM retailer_catalog_meta WHERE meta_key='schema_version'"
    ).fetchone() if _table_exists(conn, "retailer_catalog_meta") else None
    if current is not None:
        version = int(current[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"S24 schema {version} is newer than this code ({SCHEMA_VERSION})")
        if version == SCHEMA_VERSION:
            _validate_schema(conn)
            return False

    # Refuse incompatible pre-existing S24 table names before creating any object.
    for table, required in _required_columns().items():
        if _table_exists(conn, table):
            actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info(\"{table}\")")}
            missing = required - actual
            if missing:
                raise RuntimeError(f"Pre-existing S24 table {table} is incompatible: missing {sorted(missing)}")

    conn.execute("SAVEPOINT s24_schema_v1")
    try:
        statement = ""
        for line in DDL.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                statement = ""
                if sql:
                    conn.execute(sql)
        if statement.strip():
            raise sqlite3.OperationalError("Incomplete S24.3 DDL statement")
        _validate_schema(conn)
        conn.execute(
            "INSERT INTO retailer_catalog_meta(meta_key,meta_value) VALUES('schema_version',?) "
            "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
            (str(SCHEMA_VERSION),),
        )
        conn.execute("RELEASE SAVEPOINT s24_schema_v1")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK TO SAVEPOINT s24_schema_v1")
            conn.execute("RELEASE SAVEPOINT s24_schema_v1")
        except sqlite3.Error:
            pass
        raise


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _required_columns() -> dict[str, set[str]]:
    return {
        "retailer_catalog_meta": {"meta_key", "meta_value"},
        "retailer_catalog_imports": {"import_id", "retailer_key", "source_file_name", "source_sha256", "source_record_count", "import_status"},
        "retailer_catalog_products": {"catalog_product_id", "retailer_key", "external_product_id", "first_external_id_type"},
        "retailer_product_observations": {"observation_id", "import_id", "source_record_index", "retailer_key", "source_retailer_label", "catalog_product_id", "identity_status", "raw_record_json"},
        "retailer_product_identity_candidates": {"candidate_id", "observation_id", "candidate_value", "candidate_source", "candidate_status"},
        "retailer_identity_resolution_events": {"resolution_event_id", "observation_id", "catalog_product_id", "evidence_type", "evidence_reference", "actor"},
        "retailer_product_price_observations": {"price_observation_id", "observation_id", "field_name", "raw_value_json", "value_state", "semantic_role", "currency_status"},
        "retailer_product_category_observations": {"category_observation_id", "observation_id", "path_position", "raw_label_json"},
        "retailer_product_asset_observations": {"asset_observation_id", "observation_id", "asset_url", "url_classification", "verification_status"},
    }


def _validate_schema(conn: sqlite3.Connection) -> None:
    missing_tables = set(_required_columns()) - _existing_tables(conn)
    if missing_tables:
        raise RuntimeError(f"S24 schema tables are missing: {sorted(missing_tables)}")
    for table, required in _required_columns().items():
        actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info(\"{table}\")")}
        missing = required - actual
        if missing:
            raise RuntimeError(f"S24 schema table {table} has incompatible columns: {sorted(missing)}")


def _required_tables() -> set[str]:
    return set(_required_columns())


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def register_import(
    conn: sqlite3.Connection, *, retailer_key: str, source_file_name: str,
    source_sha256: str, source_record_count: int, imported_at_utc: str | None = None,
    source_size_bytes: int | None = None, source_mtime_utc: str | None = None,
) -> tuple[int, bool]:
    if retailer_key not in RETAILERS:
        raise ValueError(f"Unregistered retailer: {retailer_key}")
    if len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
        raise ValueError("source_sha256 must be a lowercase 64-character SHA-256 hex digest")
    if source_record_count < 0:
        raise ValueError("source_record_count must be >= 0")
    ts = imported_at_utc or utc_now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO retailer_catalog_imports "
        "(retailer_key,source_file_name,source_sha256,source_size_bytes,source_mtime_utc,"
        "imported_at_utc,source_record_count) VALUES(?,?,?,?,?,?,?)",
        (retailer_key, source_file_name, source_sha256, source_size_bytes,
         source_mtime_utc, ts, source_record_count),
    )
    row = conn.execute(
        "SELECT import_id,source_record_count FROM retailer_catalog_imports "
        "WHERE retailer_key=? AND source_sha256=?", (retailer_key, source_sha256)
    ).fetchone()
    if row["source_record_count"] != source_record_count:
        raise sqlite3.IntegrityError("Same retailer+SHA-256 was registered with a different record count")
    return int(row["import_id"]), cur.rowcount == 1


def _raw_type(value: Any) -> str:
    if isinstance(value, bool): return "boolean"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    if isinstance(value, str): return "string"
    if value is None: return "null"
    return "invalid"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _price_state(value: Any) -> tuple[str, str, str | None, str]:
    if value is None:
        return "null", "not_attempted", None, "unassessed"
    if isinstance(value, str) and value.strip() == "":
        return "empty", "not_numeric", None, "unassessed"
    if isinstance(value, bool):
        return "invalid", "not_numeric", None, "unassessed"
    try:
        dec = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return "invalid", "not_numeric", None, "unassessed"
    if not dec.is_finite():
        return "invalid", "not_numeric", None, "unassessed"
    normalized = format(dec, "f")
    return "value", "parsed", normalized, ("positive" if dec > 0 else "zero_or_negative")


def classify_asset_url(url: str | None) -> str:
    if not url:
        return "missing"
    from urllib.parse import urlparse
    parsed = urlparse(str(url))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "invalid_url"
    path = parsed.path.lower()
    if any(token in path for token in ("/catalog/", "/category/", "/skidki-po-karte")):
        return "listing_page"
    return "image_candidate"  # candidate only; network/content verification is not performed.


@_atomic_savepoint("s24_record_observation")
def record_observation(
    conn: sqlite3.Connection, *, import_id: int, source_record_index: int,
    canonical: dict[str, Any], created_at_utc: str | None = None,
) -> int:
    retailer = canonical.get("retailer_key")
    if retailer not in RETAILERS:
        raise ValueError(f"Unregistered retailer: {retailer}")
    status = canonical.get("identity_status")
    if status not in {"resolved", "pending", "invalid"}:
        raise ValueError(f"Invalid identity_status: {status}")
    source_id = canonical.get("external_product_id")
    raw_record_for_id = canonical.get("raw_record") if isinstance(canonical.get("raw_record"), dict) else {}
    source_id_field = canonical.get("external_product_id_source_field")
    if "external_product_id_raw" in canonical:
        raw_id_present = True
        raw_id_value = canonical.get("external_product_id_raw")
    elif source_id is not None:
        raw_id_present = True
        raw_id_value = source_id
    elif source_id_field and source_id_field in raw_record_for_id:
        raw_id_present = True
        raw_id_value = raw_record_for_id[source_id_field]
    else:
        raw_id_present = False
        raw_id_value = None
    if not raw_id_present:
        raw_id_type = "missing"
    elif raw_id_value is None:
        raw_id_type = "null"
    elif isinstance(raw_id_value, str) and raw_id_value.strip() == "":
        raw_id_type = "empty"
    else:
        raw_id_type = _raw_type(raw_id_value)
    external_id_text = str(source_id) if source_id is not None else None
    if external_id_text == "":
        raw_id_type = "empty"
    if status == "resolved" and not external_id_text:
        raise ValueError("resolved observations require a non-empty source external ID")
    if status != "resolved" and external_id_text is not None:
        raise ValueError("pending/invalid observations cannot claim a resolved external ID")
    raw_record = canonical.get("raw_record")
    if not isinstance(raw_record, dict):
        raise ValueError("raw_record must be the original source object")

    ts = created_at_utc or utc_now()
    catalog_product_id = None
    if status == "resolved":
        source_type = _raw_type(canonical.get("external_product_id_raw", source_id))
        conn.execute(
            "INSERT INTO retailer_catalog_products "
            "(retailer_key,external_product_id,first_external_id_type,first_seen_at_utc,last_seen_at_utc) "
            "VALUES(?,?,?,?,?) ON CONFLICT(retailer_key,external_product_id) DO UPDATE "
            "SET last_seen_at_utc=COALESCE(excluded.last_seen_at_utc,retailer_catalog_products.last_seen_at_utc)",
            (retailer, external_id_text, source_type, canonical.get("scraped_at_utc"), canonical.get("scraped_at_utc")),
        )
        product = conn.execute(
            "SELECT catalog_product_id FROM retailer_catalog_products WHERE retailer_key=? AND external_product_id=?",
            (retailer, external_id_text),
        ).fetchone()
        catalog_product_id = int(product[0])

    cur = conn.execute(
        "INSERT INTO retailer_product_observations "
        "(import_id,source_record_index,retailer_key,source_retailer_label,catalog_product_id,identity_status,"
        "source_external_product_id,source_external_id_raw_json,source_external_id_type,source_external_id_field,"
        "product_name,brand,product_url,image_url,uom,category_slug,category_id,scraped_at_raw,scraped_at_utc,"
        "scraped_at_status,raw_record_json,created_at_utc) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (import_id, source_record_index, retailer,
         canonical.get("source_retailer_label") or raw_record.get("retailer"),
         catalog_product_id, status, external_id_text,
         _json(raw_id_value) if raw_id_present else None,
         raw_id_type, canonical.get("external_product_id_source_field"), canonical.get("product_name"),
         canonical.get("brand"), canonical.get("product_url"), canonical.get("image_url"),
         canonical.get("uom"), canonical.get("category_slug"),
         str(canonical.get("category_id")) if canonical.get("category_id") is not None else None,
         canonical.get("scraped_at_raw"), canonical.get("scraped_at_utc"),
         canonical.get("scraped_at_status", "not_provided"), _json(raw_record), ts),
    )
    observation_id = int(cur.lastrowid)

    candidate = canonical.get("identity_candidate")
    if candidate:
        if not candidate.get("value") or not candidate.get("source"):
            raise ValueError("identity_candidate requires value and source")
        known_product = conn.execute(
            "SELECT 1 FROM retailer_catalog_products WHERE retailer_key=? AND external_product_id=?",
            (retailer, str(candidate["value"])),
        ).fetchone()
        candidate_status = "collision" if known_product else "unverified"
        conn.execute(
            "INSERT INTO retailer_product_identity_candidates "
            "(observation_id,candidate_value,candidate_source,candidate_status,created_at_utc) "
            "VALUES(?,?,?,?,?)",
            (observation_id, str(candidate["value"]), str(candidate["source"]), candidate_status, ts),
        )

    price_fields = canonical.get("price_fields", {})
    if not isinstance(price_fields, dict):
        raise ValueError("price_fields must be an object")
    currency_code = canonical.get("currency_code")
    currency_status = canonical.get("currency_status", "not_declared")
    semantic_roles = canonical.get("price_semantic_roles", {})
    if currency_status not in {"declared", "not_declared", "invalid"}:
        currency_status = "invalid"
    for field_name, value in price_fields.items():
        state, parse_status, parsed, quality = _price_state(value)
        conn.execute(
            "INSERT INTO retailer_product_price_observations "
            "(observation_id,field_name,raw_value_json,value_state,parsed_decimal_text,parse_status,value_quality,semantic_role,currency_code,currency_status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (observation_id, str(field_name), _json(value), state, parsed, parse_status, quality,
             str(semantic_roles.get(field_name, "unclassified")), currency_code, currency_status),
        )

    category_observations = canonical.get("category_observations")
    if category_observations is None:
        categories = canonical.get("category_path", [])
        if not isinstance(categories, list):
            raise ValueError("category_path must be a list")
        category_observations = [
            {"source_field": "category_path", "path_position": pos, "value": label}
            for pos, label in enumerate(categories)
        ]
    if not isinstance(category_observations, list):
        raise ValueError("category_observations must be a list")
    for item in category_observations:
        if not isinstance(item, dict) or "value" not in item:
            raise ValueError("each category observation must contain a value")
        source_field = str(item.get("source_field") or "category_path")
        path_position = int(item.get("path_position", 0))
        label = item["value"]
        conn.execute(
            "INSERT INTO retailer_product_category_observations "
            "(observation_id,source_field,path_position,raw_label_json,label_text) VALUES(?,?,?,?,?)",
            (observation_id, source_field, path_position, _json(label), label if isinstance(label, str) else None),
        )

    asset_url = canonical.get("image_url")
    asset_classification = canonical.get("image_url_classification") or classify_asset_url(asset_url)
    allowed_asset_classifications = {"image_candidate", "listing_page", "invalid_url", "missing", "unclassified_url", "invalid_type"}
    if asset_classification not in allowed_asset_classifications:
        asset_classification = "unclassified_url"
    conn.execute(
        "INSERT INTO retailer_product_asset_observations "
        "(observation_id,source_field,asset_url,url_classification,classification_method,created_at_utc) "
        "VALUES(?,?,?,?,?,?)",
        (observation_id, "image_url", asset_url, asset_classification,
         canonical.get("asset_classification_method", "s24_adapter_heuristic"), ts),
    )
    return observation_id


def finalize_import(conn: sqlite3.Connection, import_id: int, committed_record_count: int) -> None:
    if committed_record_count < 0:
        raise ValueError("committed_record_count must be >= 0")
    row = conn.execute(
        "SELECT source_record_count,import_status FROM retailer_catalog_imports WHERE import_id=?",
        (import_id,),
    ).fetchone()
    if row is None or row["import_status"] != "staged":
        raise sqlite3.IntegrityError("Import is missing or is not in staged state")
    actual = conn.execute(
        "SELECT COUNT(*) FROM retailer_product_observations WHERE import_id=?", (import_id,)
    ).fetchone()[0]
    if committed_record_count != actual or committed_record_count != row["source_record_count"]:
        raise sqlite3.IntegrityError(
            f"Import accounting mismatch: source={row['source_record_count']}, "
            f"observations={actual}, requested_commit={committed_record_count}"
        )
    conn.execute(
        "UPDATE retailer_catalog_imports SET committed_record_count=?,import_status='committed' "
        "WHERE import_id=? AND import_status='staged'",
        (committed_record_count, import_id),
    )


@_atomic_savepoint("s24_resolve_identity")
def resolve_pending_identity(
    conn: sqlite3.Connection, *, observation_id: int, external_product_id: str,
    evidence_type: str, evidence_reference: str, actor: str, note: str | None = None,
    matched_candidate_id: int | None = None, resolved_at_utc: str | None = None,
) -> int:
    """Explicit, auditable resolution. Does not infer ID from candidate automatically."""
    if evidence_type not in RESOLUTION_EVIDENCE_TYPES:
        raise ValueError("Unsupported identity resolution evidence_type")
    if not evidence_reference.strip() or not actor.strip() or not external_product_id:
        raise ValueError("external ID, evidence reference and actor are required")
    row = conn.execute(
        "SELECT retailer_key,identity_status,catalog_product_id FROM retailer_product_observations WHERE observation_id=?",
        (observation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Observation {observation_id} not found")
    if row["identity_status"] != "pending" or row["catalog_product_id"] is not None:
        raise sqlite3.IntegrityError("Only unresolved pending observations can be resolved")
    retailer = row["retailer_key"]
    if matched_candidate_id is not None:
        cand = conn.execute(
            "SELECT candidate_value,candidate_status FROM retailer_product_identity_candidates "
            "WHERE candidate_id=? AND observation_id=?", (matched_candidate_id, observation_id)
        ).fetchone()
        if cand is None or cand["candidate_status"] != "unverified" or cand["candidate_value"] != external_product_id:
            raise sqlite3.IntegrityError("Matched candidate must belong to observation and equal the verified ID")

    ts = resolved_at_utc or utc_now()
    conn.execute(
        "INSERT INTO retailer_catalog_products "
        "(retailer_key,external_product_id,first_external_id_type,first_seen_at_utc,last_seen_at_utc) "
        "VALUES(?,?, 'string',?,?) ON CONFLICT(retailer_key,external_product_id) DO UPDATE "
        "SET last_seen_at_utc=excluded.last_seen_at_utc",
        (retailer, external_product_id, ts, ts),
    )
    product_id = int(conn.execute(
        "SELECT catalog_product_id FROM retailer_catalog_products WHERE retailer_key=? AND external_product_id=?",
        (retailer, external_product_id),
    ).fetchone()[0])
    conn.execute(
        "UPDATE retailer_product_observations SET identity_status='resolved',catalog_product_id=? "
        "WHERE observation_id=? AND identity_status='pending'",
        (product_id, observation_id),
    )
    conn.execute(
        "INSERT INTO retailer_identity_resolution_events "
        "(observation_id,retailer_key,catalog_product_id,resolved_external_product_id,evidence_type,"
        "evidence_reference,actor,note,created_at_utc) VALUES(?,?,?,?,?,?,?,?,?)",
        (observation_id, retailer, product_id, external_product_id, evidence_type,
         evidence_reference, actor, note, ts),
    )
    if matched_candidate_id is not None:
        conn.execute(
            "UPDATE retailer_product_identity_candidates SET candidate_status='promoted' WHERE candidate_id=?",
            (matched_candidate_id,),
        )
    return product_id
