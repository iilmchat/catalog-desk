from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from s24.schema_v1 import apply_schema, connect
from tools.s24_globus_pilot import (
    canonical_globus_document,
    iter_json_array,
    run_pilot,
    stable_record_hash,
    validate_globus_source,
)


def write_source(path: Path, rows: list[dict]) -> None:
    path.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def seed_source_db(path: Path) -> None:
    conn = connect(str(path))
    try:
        conn.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta VALUES('schema_version','10')")
        conn.execute("CREATE TABLE products(chain TEXT,sap_code TEXT,plu TEXT,PRIMARY KEY(chain,sap_code,plu))")
        conn.execute("INSERT INTO products VALUES('5ka','35XY','4407752')")
        conn.execute("CREATE INDEX idx_products_name ON products(plu)")
        apply_schema(conn)
        conn.commit()
    finally:
        conn.close()


class GlobusCanonicalTests(unittest.TestCase):
    def test_string_id_with_leading_zeroes_and_suffix_is_preserved(self):
        raw = {
            "retailer": "globus", "product_id": "000000000000744634_ST",
            "name": "Чипсы", "category_path": ["Акции", "Чипсы", "Здоровый выбор"],
            "category_slug": "akcii", "category_id": 20,
            "price": 79.99, "order_price": 79.99, "old_price": 87.99,
            "promo_price": 79.99, "price_min": 79.99, "price_max": 87.99,
            "price_style": "special", "scraped_at": "2026-11-26T23:28:23.998033+00:00",
            "image_url": "https://image.globus.ru/mobile/20/90/4462090.jpg",
        }
        doc = canonical_globus_document(raw, 0, "globus_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["external_id"], "000000000000744634_ST")
        self.assertEqual(doc["identity"]["source_id_raw"], raw["product_id"])
        self.assertEqual(doc["categories"]["source_path"], raw["category_path"])
        self.assertEqual(doc["categories"]["source_slug"], "akcii")
        self.assertEqual(doc["price_fields"]["price"]["raw_value"], 79.99)
        self.assertEqual(doc["price_fields"]["old_price"]["raw_value"], 87.99)
        self.assertEqual(doc["links"]["image_url_classification"], "asset_candidate")
        self.assertEqual(doc["source_record_raw"], raw)

    def test_null_price_is_preserved_as_null_state(self):
        raw = {"retailer": "globus", "product_id": "001_ST", "price": None}
        doc = canonical_globus_document(raw, 0, "globus_products.json", "b" * 64)
        self.assertEqual(doc["price_fields"]["price"]["state"], "null")
        self.assertIsNone(doc["price_fields"]["price"]["raw_value"])

    def test_missing_price_is_not_materialized(self):
        raw = {"retailer": "globus", "product_id": "001_ST"}
        doc = canonical_globus_document(raw, 0, "globus_products.json", "b" * 64)
        self.assertNotIn("price", doc["price_fields"])

    def test_wrong_retailer_rejected(self):
        raw = {"retailer": "5ka", "product_id": "001"}
        with self.assertRaises(ValueError):
            validate_globus_source_from_rows([raw])

    def test_missing_product_id_rejected(self):
        raw = {"retailer": "globus", "name": "no id"}
        with self.assertRaises(ValueError):
            canonical_globus_document(raw, 0, "globus_products.json", "b" * 64)


# Small helper keeps the validator's streaming and source-hash checks in the test path.
def validate_globus_source_from_rows(rows: list[dict]) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "globus_products.json"
        write_source(path, rows)
        validate_globus_source(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               expected_count=len(rows))


class JsonArrayStreamingTests(unittest.TestCase):
    def test_streams_utf8_json_array_across_small_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"name":"Чипсы 🥔","product_id":"0001_ST"},{"name":"Кефир","product_id":"0002_ST"}]', encoding="utf-8")
            rows = list(iter_json_array(path, chunk_size=7))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["name"], "Чипсы 🥔")

    def test_rejects_non_array_root(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('{"product_id":"1"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=4))

    def test_rejects_trailing_comma(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"product_id":"1"},]', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=5))

    def test_rejects_trailing_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"product_id":"1"}] garbage', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=5))


class GlobusPilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_db = self.root / "source.sqlite3"
        self.source_json = self.root / "globus_products.json"
        self.out_dir = self.root / "pilot_out"
        seed_source_db(self.source_db)
        self.rows = [
            {
                "retailer": "globus", "product_id": "000000000000744634_ST",
                "name": "Чипсы Dr. Körner", "category_path": ["Акции", "Чипсы", "Здоровый выбор"],
                "category_slug": "akcii", "category_id": 20,
                "price": 79.99, "order_price": 79.99, "old_price": 87.99,
                "promo_price": 79.99, "price_style": "special", "price_min": 79.99,
                "price_max": 87.99, "uom": "1 шт", "package_type": "piece",
                "in_stock": True, "image_url": "https://image.globus.ru/mobile/20/90/a.jpg",
                "scraped_at": "2026-11-26T23:28:23.998033+00:00",
            },
            {
                "retailer": "globus", "product_id": "000000000000746791_ST",
                "name": "Каша Yelli", "category_path": ["Акции", "Бакалея", "Здоровый выбор"],
                "category_slug": "akcii", "category_id": 20,
                "price": 73.99, "order_price": 73.99, "old_price": 109.99,
                "promo_price": 73.99, "price_style": "special+regular_crossed",
                "price_min": 73.99, "price_max": 109.99, "uom": "1 шт",
                "image_url": "https://image.globus.ru/mobile/57/97/b.jpg",
            },
            {
                "retailer": "globus", "product_id": "000000000000475349_ST",
                "name": "Набор ёлочных шаров", "category_path": ["Акции", "Новый год"],
                "category_slug": "akcii", "category_id": 20,
                "price": 472.99, "order_price": 472.99, "old_price": 559.99,
                "promo_price": 472.99, "price_style": "special", "price_min": 472.99,
                "price_max": 559.99, "uom": "1 уп", "image_url": "https://image.globus.ru/mobile/57/92/c.jpg",
            },
        ]
        write_source(self.source_json, self.rows)
        self.source_hash = hashlib.sha256(self.source_json.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_pilot_imports_only_globus_and_repeat_is_noop(self):
        report = run_pilot(self.source_db, self.source_json, self.out_dir,
                           expected_source_sha256=self.source_hash, expected_source_count=3)
        self.assertTrue(report["production_database_modified"] is False)
        self.assertEqual(report["globus_counts"]["confirmed_products"], 3)
        self.assertEqual(report["globus_counts"]["observations"], 3)
        self.assertEqual(report["globus_counts"]["resolved_identity_records"], 3)
        self.assertEqual(report["globus_counts"]["price_observations"], 18)
        self.assertEqual(report["globus_counts"]["category_observations"], 8)
        self.assertEqual(report["globus_counts"]["asset_observations"], 3)
        self.assertTrue(report["idempotency_noop_confirmed"])
        self.assertTrue(report["legacy_objects_and_row_counts_unchanged"])
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertEqual(report["sqlite_quick_check"], "ok")

        conn = sqlite3.connect(self.out_dir / "globus_pilot.sqlite3")
        try:
            ids = [r[0] for r in conn.execute(
                "SELECT external_product_id FROM retailer_catalog_products WHERE retailer_key='globus' ORDER BY external_product_id"
            )]
            self.assertIn("000000000000744634_ST", ids)
            first = conn.execute(
                "SELECT raw_record_json FROM retailer_product_observations WHERE source_record_index=0"
            ).fetchone()[0]
            self.assertIn('"price_style":"special"', first)
            null_states = conn.execute(
                "SELECT COUNT(*) FROM retailer_product_price_observations p "
                "JOIN retailer_product_observations o USING(observation_id) "
                "WHERE o.retailer_key='globus' AND p.value_state='null'"
            ).fetchone()[0]
            self.assertEqual(null_states, 0)
        finally:
            conn.close()

    def test_existing_output_directory_refused(self):
        self.out_dir.mkdir()
        with self.assertRaises(FileExistsError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                       expected_source_sha256=self.source_hash, expected_source_count=3)

    def test_source_hash_mismatch_refused(self):
        with self.assertRaises(ValueError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                       expected_source_sha256="0" * 64, expected_source_count=3)

    def test_duplicate_ids_rejected_before_database_copy(self):
        dup = dict(self.rows[0])
        dup["name"] = "Duplicate ID"
        write_source(self.source_json, self.rows + [dup])
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                       expected_source_sha256=digest, expected_source_count=4)
        self.assertFalse((self.out_dir / "globus_pilot.sqlite3").exists())

    def test_wrong_retailer_in_file_rejected(self):
        bad = [dict(self.rows[0], retailer="5ka")]
        write_source(self.source_json, bad)
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                       expected_source_sha256=digest, expected_source_count=1)

    def test_source_database_remains_unchanged(self):
        before = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        run_pilot(self.source_db, self.source_json, self.out_dir,
                  expected_source_sha256=digest, expected_source_count=3)
        after = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
