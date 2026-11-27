from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from s24.schema_v1 import apply_schema, connect
from tools.s24_dixy_pilot import (
    EXPECTED_SOURCE_RECORDS,
    EXPECTED_SOURCE_SHA256,
    canonical_dixy_document,
    dixy_url_id_candidate,
    iter_json_array,
    run_pilot,
    validate_dixy_source,
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
        conn.execute("CREATE TABLE enrichment_batch_states(chain TEXT PRIMARY KEY,active INTEGER NOT NULL)")
        conn.execute("INSERT INTO enrichment_batch_states VALUES('5ka',0)")
        apply_schema(conn)
        conn.commit()
    finally:
        conn.close()


class DixyCanonicalTests(unittest.TestCase):

    def test_pinned_snapshot_matches_latest_read_only_profile(self):
        self.assertEqual(EXPECTED_SOURCE_SHA256,
                         "8295a61ee1da54ff27638dac2c0d592f1eef59a2caa8362b964af12e1d765777")
        self.assertEqual(EXPECTED_SOURCE_RECORDS, 15311)

    def test_confirmed_string_id_is_preserved(self):
        raw = {"retailer": "dixy", "product_id": "2000538475", "name": "Зефир",
               "url": "https://dixy.ru/product/zefir-2000538475/", "price_min": 139.9,
               "price_max": 139.9, "category_name": "Зефир", "category_slug": "skidki-po-karte"}
        doc = canonical_dixy_document(raw, 0, "dixy_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["status"], "resolved")
        self.assertEqual(doc["identity"]["external_id"], "2000538475")
        self.assertEqual(doc["identity"]["source_id_raw"], "2000538475")
        self.assertIsNone(doc["identity"]["candidate"])

    def test_missing_id_stays_pending_and_url_candidate_is_not_promoted(self):
        raw = {"retailer": "dixy", "name": "Блины", "url": "https://dixy.ru/product/bliny-2000651251/"}
        doc = canonical_dixy_document(raw, 0, "dixy_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["status"], "pending")
        self.assertIsNone(doc["identity"]["external_id"])
        self.assertFalse(doc["identity"]["source_id_present"])
        self.assertEqual(doc["identity"]["candidate"]["external_id_candidate"], "2000651251")
        self.assertFalse(doc["identity"]["candidate"]["verified"])

    def test_explicit_null_id_remains_distinct_from_missing(self):
        raw = {"retailer": "dixy", "product_id": None, "url": "https://dixy.ru/product/kefir-2000650992/"}
        doc = canonical_dixy_document(raw, 0, "dixy_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["status"], "pending")
        self.assertTrue(doc["identity"]["source_id_present"])
        self.assertIsNone(doc["identity"]["source_id_raw"])
        self.assertEqual(doc["identity"]["candidate"]["external_id_candidate"], "2000650992")

    def test_product_key_is_not_used_as_candidate(self):
        raw = {"retailer": "dixy", "product_key": "/product/example-2000651251/"}
        doc = canonical_dixy_document(raw, 0, "dixy_products.json", "a" * 64)
        self.assertIsNone(doc["identity"]["candidate"])

    def test_candidate_requires_dixy_host_and_terminal_ten_digit_path_id(self):
        self.assertEqual(dixy_url_id_candidate("https://dixy.ru/product/example-2000651251/"), "2000651251")
        self.assertEqual(dixy_url_id_candidate("https://www.dixy.ru/product/example-2000651251"), "2000651251")
        self.assertIsNone(dixy_url_id_candidate("https://other.example/product/example-2000651251/"))
        self.assertIsNone(dixy_url_id_candidate("https://dixy.ru/catalog/example-2000651251/"))
        self.assertIsNone(dixy_url_id_candidate("https://dixy.ru/product/example-12345/"))

    def test_invalid_boolean_numeric_and_object_ids_are_rejected(self):
        for value in (True, 1.5, {"id": "2000000001"}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    canonical_dixy_document({"retailer": "dixy", "product_id": value}, 0, "x", "a" * 64)

    def test_missing_price_is_not_materialized_and_null_preserved(self):
        raw = {"retailer": "dixy", "product_id": "2000538475", "price_min": None}
        doc = canonical_dixy_document(raw, 0, "dixy_products.json", "a" * 64)
        self.assertNotIn("price", doc["price_fields"])
        self.assertEqual(doc["price_fields"]["price_min"]["state"], "null")

    def test_category_name_and_slug_are_separate(self):
        raw = {"retailer": "dixy", "product_id": "2000538475", "category_name": "Зефир",
               "category_slug": "skidki-po-karte"}
        doc = canonical_dixy_document(raw, 0, "dixy_products.json", "a" * 64)
        self.assertEqual(doc["categories"]["source_name"], "Зефир")
        self.assertEqual(doc["categories"]["source_slug"], "skidki-po-karte")

    def test_wrong_retailer_rejected(self):
        with self.assertRaises(ValueError):
            canonical_dixy_document({"retailer": "globus", "product_id": "1"}, 0, "x", "a" * 64)


class JsonArrayStreamingTests(unittest.TestCase):
    def test_streams_utf8_json_array_across_small_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "source.json"
            path.write_text('[{"name":"Зефир 🍓","product_id":"2000538475"}]', encoding="utf-8")
            self.assertEqual(len(list(iter_json_array(path, chunk_size=7))), 1)

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
            path.write_text('[{"product_id":"1"}] extra', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(path, chunk_size=5))


class DixyPilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source_db = self.root / "source.sqlite3"
        self.source_json = self.root / "dixy_products.json"
        self.out_dir = self.root / "pilot_out"
        seed_source_db(self.source_db)
        self.rows = [
            {"retailer": "dixy", "product_id": "2000000001", "name": "Товар с ID",
             "url": "https://dixy.ru/product/item-2000000001/", "price_min": 49.9,
             "price_max": 49.9, "price_old": None, "category_slug": "skidki-po-karte",
             "category_name": "Снеки", "image_url": "https://dixy.ru/upload/item.webp"},
            {"retailer": "dixy", "name": "Pending with candidate",
             "url": "https://dixy.ru/product/candidate-2000000002/", "price_min": 69.9,
             "price_max": 69.9, "category_name": "Снеки"},
            {"retailer": "dixy", "product_id": None, "name": "Pending collision",
             "url": "https://dixy.ru/product/collision-2000000001/", "price_min": None,
             "image_url": "https://dixy.ru/catalog/skidki-po-karte/"},
            {"retailer": "dixy", "name": "Pending no candidate",
             "product_key": "/product/path-2000000003/", "url": "https://dixy.ru/product/no-id/"},
        ]
        write_source(self.source_json, self.rows)
        self.source_hash = hashlib.sha256(self.source_json.read_bytes()).hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def test_pilot_imports_pending_and_candidates_without_promoting_then_repeat_noop(self):
        report = run_pilot(self.source_db, self.source_json, self.out_dir,
                           expected_source_sha256=self.source_hash, expected_source_count=4)
        c = report["dixy_counts"]
        self.assertFalse(report["production_database_modified"])
        self.assertEqual(c["observations"], 4)
        self.assertEqual(c["confirmed_products"], 1)
        self.assertEqual(c["pending_records"], 3)
        self.assertEqual(c["url_identity_candidate_records"], 2)
        self.assertEqual(c["candidate_known_id_collisions_preflight"], 1)
        self.assertTrue(report["idempotency_noop_confirmed"])
        self.assertTrue(report["legacy_objects_and_row_counts_unchanged"])
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertEqual(report["sqlite_quick_check"], "ok")
        conn = sqlite3.connect(self.out_dir / "dixy_pilot.sqlite3")
        try:
            candidates = conn.execute(
                "SELECT candidate_value,candidate_status FROM retailer_product_identity_candidates ORDER BY candidate_value"
            ).fetchall()
            self.assertEqual(candidates, [("2000000001", "collision"), ("2000000002", "unverified")])
            products = conn.execute(
                "SELECT external_product_id FROM retailer_catalog_products WHERE retailer_key='dixy'"
            ).fetchall()
            self.assertEqual(products, [("2000000001",)])
            pending = conn.execute(
                "SELECT COUNT(*) FROM retailer_product_observations WHERE retailer_key='dixy' AND identity_status='pending'"
            ).fetchone()[0]
            self.assertEqual(pending, 3)
        finally:
            conn.close()

    def test_existing_output_directory_refused(self):
        self.out_dir.mkdir()
        with self.assertRaises(FileExistsError):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                      expected_source_sha256=self.source_hash, expected_source_count=4)

    def test_source_hash_mismatch_refused_before_copy(self):
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            run_pilot(self.source_db, self.source_json, self.out_dir,
                      expected_source_sha256="0" * 64, expected_source_count=4)
        self.assertFalse(self.out_dir.exists())

    def test_duplicate_confirmed_ids_rejected_before_database_copy(self):
        rows = [dict(self.rows[0]), dict(self.rows[0])]
        write_source(self.source_json, rows)
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Duplicate Dixy product_id"):
            validate_dixy_source(self.source_json, digest, 2)

    def test_wrong_retailer_in_file_rejected(self):
        write_source(self.source_json, [{"retailer": "globus", "product_id": "1"}])
        digest = hashlib.sha256(self.source_json.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "Unexpected retailer"):
            validate_dixy_source(self.source_json, digest, 1)

    def test_source_database_remains_unchanged(self):
        before = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        report = run_pilot(self.source_db, self.source_json, self.out_dir,
                           expected_source_sha256=self.source_hash, expected_source_count=4)
        after = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(report["source_db_fingerprint_before_after"][0],
                         report["source_db_fingerprint_before_after"][1])


if __name__ == "__main__":
    unittest.main()
