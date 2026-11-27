# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from s24.schema_v1 import apply_schema, connect
from s24.read_model_v1 import (
    get_observation,
    get_summary,
    list_imports,
    list_observations,
    open_readonly,
)


class S24ReadModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "s24.sqlite3"
        conn = connect(str(self.db))
        try:
            apply_schema(conn)
            conn.execute(
                """
                INSERT INTO retailer_catalog_imports(
                    retailer_key,source_file_name,source_sha256,source_size_bytes,
                    source_mtime_utc,imported_at_utc,source_record_count,
                    committed_record_count,import_status
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                ("dixy", "dixy_products.json", "a" * 64, 123, None,
                 "2026-11-27T00:00:00+00:00", 2, 2, "committed"),
            )
            conn.execute(
                """
                INSERT INTO retailer_catalog_products(
                    retailer_key,external_product_id,first_external_id_type,
                    first_seen_at_utc,last_seen_at_utc
                ) VALUES(?,?,?,?,?)
                """,
                ("dixy", "2000538475", "string", None, None),
            )
            product_id = conn.execute(
                "SELECT catalog_product_id FROM retailer_catalog_products"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO retailer_product_observations(
                    import_id,source_record_index,retailer_key,source_retailer_label,
                    catalog_product_id,identity_status,source_external_product_id,
                    source_external_id_raw_json,source_external_id_type,
                    source_external_id_field,product_name,brand,product_url,image_url,
                    category_slug,raw_record_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (1, 0, "dixy", "dixy", product_id, "resolved", "2000538475",
                 '"2000538475"', "string", "product_id", "Зефир", "Dolchemania",
                 "https://dixy.ru/product/example/", "https://cdn.example/image.jpg",
                 "sweets", json.dumps({"product_id": "2000538475", "name": "Зефир"}, ensure_ascii=False),
                 "2026-11-27T00:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO retailer_product_observations(
                    import_id,source_record_index,retailer_key,source_retailer_label,
                    identity_status,source_external_id_type,raw_record_json,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (1, 1, "dixy", "dixy", "pending", "missing",
                 json.dumps({"name": "Товар без ID"}, ensure_ascii=False),
                 "2026-11-27T00:00:01+00:00"),
            )
            obs_ids = [r[0] for r in conn.execute(
                "SELECT observation_id FROM retailer_product_observations ORDER BY observation_id"
            )]
            conn.execute(
                """
                INSERT INTO retailer_product_price_observations(
                    observation_id,field_name,raw_value_json,value_state,parsed_decimal_text,
                    parse_status,value_quality,semantic_role,currency_status
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (obs_ids[0], "price_min", "139.9", "value", "139.9", "parsed",
                 "positive", "unclassified", "not_declared"),
            )
            conn.execute(
                """
                INSERT INTO retailer_product_category_observations(
                    observation_id,source_field,path_position,raw_label_json,label_text
                ) VALUES(?,?,?,?,?)
                """,
                (obs_ids[0], "breadcrumbs", 0, '"Кондитерские изделия"', "Кондитерские изделия"),
            )
            conn.execute(
                """
                INSERT INTO retailer_product_asset_observations(
                    observation_id,source_field,asset_url,url_classification,
                    classification_method,verification_status,created_at_utc
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (obs_ids[0], "image_url", "https://cdn.example/image.jpg",
                 "image_candidate", "local_heuristic", "unverified",
                 "2026-11-27T00:00:00+00:00"),
            )
            conn.execute(
                """
                INSERT INTO retailer_product_identity_candidates(
                    observation_id,candidate_value,candidate_source,candidate_status,created_at_utc
                ) VALUES(?,?,?,?,?)
                """,
                (obs_ids[1], "2000538475", "dixy-terminal-url-id-v1", "unverified",
                 "2026-11-27T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_summary_aggregates_network_counts_without_merging_identities(self):
        data = get_summary(self.db)
        self.assertTrue(data["available"])
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["totals"]["observations"], 2)
        self.assertEqual(data["totals"]["confirmed_products"], 1)
        self.assertEqual(data["totals"]["pending_observations"], 1)
        self.assertEqual(data["totals"]["identity_candidates"], 1)
        dixy = next(row for row in data["retailers"] if row["retailer_key"] == "dixy")
        self.assertEqual(dixy["observations"], 2)
        self.assertEqual(dixy["confirmed_products"], 1)
        self.assertEqual(dixy["pending_observations"], 1)
        self.assertEqual(dixy["unverified_candidates"], 1)

    def test_observation_list_filters_and_returns_separate_raw_price_fields(self):
        data = list_observations(self.db, retailer_key="dixy", identity_status="pending")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["identity_status"], "pending")
        resolved = list_observations(self.db, query="Зефир")
        self.assertEqual(resolved["total"], 1)
        item = resolved["items"][0]
        self.assertEqual(item["price_fields"][0]["field_name"], "price_min")
        self.assertEqual(item["price_fields"][0]["semantic_role"], "unclassified")
        self.assertEqual(item["price_fields"][0]["currency_status"], "not_declared")
        self.assertEqual(item["categories"][0]["label_text"], "Кондитерские изделия")
        self.assertEqual(item["assets"][0]["verification_status"], "unverified")

    def test_observation_detail_returns_raw_record_and_candidate_evidence(self):
        listing = list_observations(self.db, identity_status="pending")
        observation_id = listing["items"][0]["observation_id"]
        data = get_observation(self.db, observation_id)
        self.assertEqual(data["identity_status"], "pending")
        self.assertEqual(data["raw_record"]["name"], "Товар без ID")
        self.assertEqual(data["identity_candidates"][0]["candidate_status"], "unverified")

    def test_import_list_is_read_only_metadata(self):
        data = list_imports(self.db)
        self.assertTrue(data["available"])
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["source_record_count"], 2)

    def test_read_connection_rejects_writes_and_leaves_database_bytes_unchanged(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        conn = open_readonly(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM retailer_product_observations")
        finally:
            conn.close()
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_uninstalled_schema_is_reported_without_attempting_migration(self):
        empty = Path(self.temp.name) / "empty.sqlite3"
        conn = sqlite3.connect(empty)
        conn.close()
        result = get_summary(empty)
        self.assertFalse(result["available"])
        self.assertIn("S24 schema is not installed", result["reason"])


if __name__ == "__main__":
    unittest.main()
