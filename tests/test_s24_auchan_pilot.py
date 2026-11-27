from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from s24.schema_v1 import apply_schema, connect
from tools.s24_auchan_pilot import (
    canonical_auchan_document,
    iter_json_array,
    run_pilot,
    validate_auchan_source,
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


class AuchanCanonicalTests(unittest.TestCase):
    def test_numeric_id_is_stringified_but_source_type_is_preserved(self):
        raw = {
            "retailer": "auchan", "product_id": 628149,
            "name": "Вино Kindzmarauli Maran", "brand": "Kindzmarauli Maran",
            "category_path": ["Алкоголь (самовывоз)", "Вино", "Красное вино"],
            "category_slug": "alkogol", "url": "https://www.auchan.ru/product/example/",
            "image_url": "https://www.auchan.ru/files/original/36330468",
            "price": 1499.97, "old_price": 2799.99, "discount_percent": 46,
            "price_min": 1499.97, "price_max": 2799.99, "uom": "item",
            "scraped_at": "2026-11-26T23:28:19.473364+00:00",
        }
        doc = canonical_auchan_document(raw, 0, "auchan_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["external_id"], "628149")
        self.assertEqual(doc["identity"]["source_id_raw"], 628149)
        self.assertEqual(doc["categories"]["source_path"], raw["category_path"])
        self.assertEqual(doc["categories"]["source_slug"], "alkogol")
        self.assertEqual(doc["price_fields"]["price"]["raw_value"], 1499.97)
        self.assertEqual(doc["price_fields"]["old_price"]["raw_value"], 2799.99)
        self.assertEqual(doc["price_fields"]["discount_percent"]["raw_value"], 46)
        self.assertEqual(doc["source_record_raw"], raw)

    def test_null_price_is_preserved_as_null_state(self):
        raw = {"retailer": "auchan", "product_id": 123, "old_price": None}
        doc = canonical_auchan_document(raw, 0, "auchan_products.json", "b" * 64)
        self.assertEqual(doc["price_fields"]["old_price"]["state"], "null")
        self.assertIsNone(doc["price_fields"]["old_price"]["raw_value"])

    def test_missing_price_is_not_materialized(self):
        raw = {"retailer": "auchan", "product_id": 123}
        doc = canonical_auchan_document(raw, 0, "auchan_products.json", "b" * 64)
        self.assertNotIn("price", doc["price_fields"])
        self.assertNotIn("old_price", doc["price_fields"])

    def test_wrong_retailer_rejected(self):
        with self.assertRaises(ValueError):
            canonical_auchan_document({"retailer": "globus", "product_id": 1}, 0, "x", "c" * 64)

    def test_missing_product_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_auchan_document({"retailer": "auchan", "name": "no id"}, 0, "x", "c" * 64)

    def test_boolean_product_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_auchan_document({"retailer": "auchan", "product_id": True}, 0, "x", "c" * 64)


class JsonArrayStreamingTests(unittest.TestCase):
    def test_streams_utf8_json_array_across_small_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"name":"Вино 🍷","product_id":628149}]', encoding="utf-8")
            rows = list(iter_json_array(path, chunk_size=7))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], "Вино 🍷")

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


class AuchanPilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_db = self.root / "source.sqlite3"
        self.source_json = self.root / "auchan_products.json"
        self.out_dir = self.root / "pilot_out"
        seed_source_db(self.source_db)
        self.rows = [
            {
                "retailer": "auchan", "product_id": 628149,
                "name": "Вино Kindzmarauli Maran", "brand": "Kindzmarauli Maran",
                "category_path": ["Алкоголь (самовывоз)", "Вино", "Красное вино"],
                "category_slug": "alkogol", "url": "https://www.auchan.ru/product/example/",
                "image_url": "https://www.auchan.ru/files/original/36330468",
                "price": 1499.97, "old_price": 2799.99, "discount_percent": 46,
                "price_min": 1499.97, "price_max": 2799.99, "uom": "item",
                "scraped_at": "2026-11-26T23:28:19.473364+00:00",
            },
            {
                "retailer": "auchan", "product_id": 84388,
                "name": "Игристое вино Martini Asti", "brand": "Martini",
                "category_path": ["Алкоголь (самовывоз)", "Игристое вино", "Игристое вино"],
                "category_slug": "alkogol", "url": "https://www.auchan.ru/product/asti/",
                "image_url": "https://www.auchan.ru/files/original/38408553",
                "price": 1599.99, "old_price": None, "discount_percent": 0,
                "price_min": 1599.99, "price_max": 1599.99, "uom": "item",
            },
            {
                "retailer": "auchan", "product_id": 84389,
                "name": "Вода", "category_path": ["Вода", "Питьевая вода"],
                "category_slug": "napitki", "price": 69.9,
                "image_url": None,
            },
        ]
        write_source(self.source_json, self.rows)
        self.source_hash = hashlib.sha256(self.source_json.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_pilot_imports_only_auchan_and_repeat_is_noop(self):
        report = run_pilot(self.source_db, self.source_json, self.out_dir,
                           expected_source_sha256=self.source_hash, expected_source_count=3)
        self.assertFalse(report["production_database_modified"])
        self.assertEqual(report["auchan_counts"]["confirmed_products"], 3)
        self.assertEqual(report["auchan_counts"]["observations"], 3)
        self.assertEqual(report["auchan_counts"]["resolved_identity_records"], 3)
        self.assertEqual(report["auchan_counts"]["price_observations"], 11)
        self.assertEqual(report["auchan_counts"]["category_observations"], 8)
        self.assertEqual(report["auchan_counts"]["asset_observations"], 3)
        self.assertTrue(report["idempotency_noop_confirmed"])
        self.assertTrue(report["legacy_objects_and_row_counts_unchanged"])
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertEqual(report["sqlite_quick_check"], "ok")

        conn = sqlite3.connect(self.out_dir / "auchan_pilot.sqlite3")
        try:
            row = conn.execute(
                "SELECT external_product_id,first_external_id_type FROM retailer_catalog_products "
                "WHERE retailer_key='auchan' AND external_product_id='628149'"
            ).fetchone()
            self.assertEqual(row, ("628149", "integer"))
            raw = conn.execute(
                "SELECT raw_record_json FROM retailer_product_observations WHERE source_record_index=0"
            ).fetchone()[0]
            self.assertIn('"product_id":628149', raw)
            self.assertIn('"category_slug":"alkogol"', raw)
            nulls = conn.execute(
                "SELECT COUNT(*) FROM retailer_product_price_observations p "
                "JOIN retailer_product_observations o USING(observation_id) "
                "WHERE o.retailer_key='auchan' AND p.field_name='old_price' AND p.value_state='null'"
            ).fetchone()[0]
            self.assertEqual(nulls, 1)
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
        duplicate = dict(self.rows[0])
        duplicate["name"] = "Duplicate"
        write_source(self.source_json, self.rows + [duplicate])
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                      expected_source_sha256=digest, expected_source_count=4)
        self.assertFalse((self.out_dir / "auchan_pilot.sqlite3").exists())

    def test_wrong_retailer_in_file_rejected(self):
        write_source(self.source_json, [dict(self.rows[0], retailer="globus")])
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                      expected_source_sha256=digest, expected_source_count=1)

    def test_source_database_remains_unchanged(self):
        before = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        run_pilot(self.source_db, self.source_json, self.out_dir,
                  expected_source_sha256=self.source_hash, expected_source_count=3)
        after = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
