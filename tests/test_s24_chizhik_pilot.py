from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from s24.schema_v1 import apply_schema, connect
from tools.s24_chizhik_pilot import (
    canonical_chizhik_document,
    iter_json_array,
    run_pilot,
    validate_chizhik_source,
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


def fixture_rows() -> list[dict]:
    return [
        {
            "retailer": "chizhik", "product_id": 4424136,
            "name": "Суп Бон Меню Лапша куриная замороженный, 300г",
            "brand": "Бон Меню",
            "category_path": ["Готовая заморозка", "Замороженные продукты"],
            "url": "https://chizhik.club/product/4424136/",
            "image_url": "https://catalog-images.x5static.net/example.jpg",
            "price": "149.0", "promo_price": None,
            "uom": "шт", "store_id": "HBBN", "stock_limit": "5.00",
            "rating": 4.8, "rates_count": 12, "max_qty_per_order": 5,
            "scraped_at": "2026-11-27T10:00:00+03:00",
        },
        {
            "retailer": "chizhik", "product_id": 4359664,
            "name": "Товар без цены", "category_path": ["Бакалея"],
            "url": "https://chizhik.club/product/4359664/",
            "image_url": None, "price": None, "promo_price": None,
            "in_stock": False, "store_id": "HBBN", "stock_limit": "5.00",
        },
        {
            "retailer": "chizhik", "product_id": 4359665,
            "name": "Третий товар", "price": "0", "promo_price": None,
            "url": "https://chizhik.club/product/4359665/",
            "image_url": "https://cdn.example.test/image.webp",
        },
    ]


class ChizhikCanonicalTests(unittest.TestCase):
    def test_integer_id_is_stringified_and_raw_id_and_retail_data_are_preserved(self):
        raw = fixture_rows()[0]
        doc = canonical_chizhik_document(raw, 0, "chizhik_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["external_id"], "4424136")
        self.assertEqual(doc["identity"]["source_id_raw"], 4424136)
        self.assertEqual(doc["price_fields"]["price"]["raw_value"], "149.0")
        self.assertEqual(doc["price_fields"]["price"]["semantic_role"], "unclassified")
        self.assertEqual(doc["price_fields"]["promo_price"]["state"], "null")
        self.assertEqual(doc["categories"]["source_path"], raw["category_path"])
        self.assertEqual(doc["source_record_raw"], raw)
        self.assertEqual(doc["source_record_raw"]["store_id"], "HBBN")
        self.assertEqual(doc["source_record_raw"]["stock_limit"], "5.00")
        self.assertEqual(doc["source_record_raw"]["rating"], 4.8)

    def test_null_and_value_price_states_remain_distinct(self):
        raw_null = fixture_rows()[1]
        raw_value = fixture_rows()[0]
        null_doc = canonical_chizhik_document(raw_null, 1, "chizhik_products.json", "b" * 64)
        value_doc = canonical_chizhik_document(raw_value, 0, "chizhik_products.json", "b" * 64)
        self.assertEqual(null_doc["price_fields"]["price"]["state"], "null")
        self.assertEqual(value_doc["price_fields"]["price"]["state"], "value")
        self.assertEqual(null_doc["price_fields"]["promo_price"]["state"], "null")

    def test_missing_price_is_not_materialized(self):
        raw = {"retailer": "chizhik", "product_id": 1}
        doc = canonical_chizhik_document(raw, 0, "chizhik_products.json", "c" * 64)
        self.assertNotIn("price", doc["price_fields"])
        self.assertNotIn("promo_price", doc["price_fields"])

    def test_wrong_retailer_rejected(self):
        with self.assertRaises(ValueError):
            canonical_chizhik_document({"retailer": "globus", "product_id": 1}, 0, "x", "d" * 64)

    def test_missing_product_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_chizhik_document({"retailer": "chizhik", "name": "no id"}, 0, "x", "e" * 64)

    def test_string_id_rejected_even_when_numeric_looking(self):
        with self.assertRaises(ValueError):
            canonical_chizhik_document({"retailer": "chizhik", "product_id": "4424136"}, 0, "x", "f" * 64)

    def test_boolean_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_chizhik_document({"retailer": "chizhik", "product_id": True}, 0, "x", "0" * 64)


class JsonArrayStreamingTests(unittest.TestCase):
    def test_streams_utf8_json_array_across_small_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"name":"Суп 🍲","product_id":4424136}]', encoding="utf-8")
            rows = list(iter_json_array(path, chunk_size=7))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Суп 🍲")

    def test_rejects_non_array_root(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('{"product_id":1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=4))

    def test_rejects_trailing_comma(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"product_id":1},]', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=5))

    def test_rejects_trailing_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"product_id":1}] garbage', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=5))


class ChizhikPilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_db = self.root / "source.sqlite3"
        self.source_json = self.root / "chizhik_products.json"
        self.out_dir = self.root / "pilot_out"
        seed_source_db(self.source_db)
        self.rows = fixture_rows()
        write_source(self.source_json, self.rows)
        self.source_hash = hashlib.sha256(self.source_json.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def _run(self):
        return run_pilot(
            self.source_db, self.source_json, self.out_dir,
            expected_source_sha256=self.source_hash,
            expected_source_count=len(self.rows),
            enforce_reviewed_profile=False,
        )

    def test_pilot_imports_chizhik_and_repeat_is_noop(self):
        report = self._run()
        self.assertFalse(report["production_database_modified"])
        self.assertEqual(report["chizhik_counts"]["confirmed_products"], 3)
        self.assertEqual(report["chizhik_counts"]["observations"], 3)
        self.assertEqual(report["chizhik_counts"]["resolved_identity_records"], 3)
        self.assertEqual(report["chizhik_counts"]["price_observations"], 6)
        self.assertEqual(report["chizhik_counts"]["category_observations"], 3)
        self.assertEqual(report["chizhik_counts"]["asset_observations"], 3)
        self.assertTrue(report["idempotency_noop_confirmed"])
        self.assertTrue(report["legacy_objects_and_row_counts_unchanged"])
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertEqual(report["sqlite_quick_check"], "ok")

        conn = sqlite3.connect(self.out_dir / "chizhik_pilot.sqlite3")
        try:
            row = conn.execute(
                "SELECT external_product_id,first_external_id_type FROM retailer_catalog_products "
                "WHERE retailer_key='chizhik' AND external_product_id='4424136'"
            ).fetchone()
            self.assertEqual(row, ("4424136", "integer"))
            raw = conn.execute(
                "SELECT raw_record_json FROM retailer_product_observations "
                "WHERE retailer_key='chizhik' AND source_record_index=0"
            ).fetchone()[0]
            self.assertIn('"product_id":4424136', raw)
            self.assertIn('"store_id":"HBBN"', raw)
            self.assertIn('"stock_limit":"5.00"', raw)
            price_raw = conn.execute(
                "SELECT raw_value_json FROM retailer_product_price_observations p "
                "JOIN retailer_product_observations o USING(observation_id) "
                "WHERE o.retailer_key='chizhik' AND o.source_record_index=0 AND p.field_name='price'"
            ).fetchone()[0]
            self.assertEqual(json.loads(price_raw), "149.0")
            nulls = conn.execute(
                "SELECT COUNT(*) FROM retailer_product_price_observations p "
                "JOIN retailer_product_observations o USING(observation_id) "
                "WHERE o.retailer_key='chizhik' AND p.field_name='promo_price' AND p.value_state='null'"
            ).fetchone()[0]
            self.assertEqual(nulls, 3)
        finally:
            conn.close()

    def test_existing_output_directory_refused(self):
        self.out_dir.mkdir()
        with self.assertRaises(FileExistsError):
            self._run()

    def test_source_hash_mismatch_refused(self):
        with self.assertRaises(ValueError):
            run_pilot(
                self.source_db, self.source_json, self.out_dir,
                expected_source_sha256="0" * 64,
                expected_source_count=len(self.rows),
                enforce_reviewed_profile=False,
            )
        self.assertFalse((self.out_dir / "chizhik_pilot.sqlite3").exists())

    def test_duplicate_ids_rejected_before_database_copy(self):
        duplicate = dict(self.rows[0])
        duplicate["name"] = "Duplicate"
        write_source(self.source_json, self.rows + [duplicate])
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            run_pilot(
                self.source_db, self.source_json, self.out_dir,
                expected_source_sha256=digest,
                expected_source_count=4,
                enforce_reviewed_profile=False,
            )
        self.assertFalse((self.out_dir / "chizhik_pilot.sqlite3").exists())

    def test_wrong_retailer_rejected_before_database_copy(self):
        write_source(self.source_json, [dict(self.rows[0], retailer="globus")])
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            run_pilot(
                self.source_db, self.source_json, self.out_dir,
                expected_source_sha256=digest,
                expected_source_count=1,
                enforce_reviewed_profile=False,
            )
        self.assertFalse((self.out_dir / "chizhik_pilot.sqlite3").exists())

    def test_source_database_remains_unchanged(self):
        before = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        self._run()
        after = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_reviewed_profile_mode_rejects_a_non_profile_snapshot(self):
        with self.assertRaises(ValueError):
            validate_chizhik_source(
                self.source_json,
                expected_sha256=self.source_hash,
                expected_count=3,
                enforce_reviewed_profile=True,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
