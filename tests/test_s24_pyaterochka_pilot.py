import json
import tempfile
import unittest
from pathlib import Path

from tools.s24_pyaterochka_pilot import (
    RETAILER, SOURCE_RETAILER_VALUE, EXPECTED_SOURCE_SHA256, EXPECTED_SOURCE_RECORDS,
    validate_pyaterochka_source, canonical_pyaterochka_document,
    iter_json_array, stable_record_hash,
)

class PyaterochkaCanonicalTests(unittest.TestCase):
    def sample(self, **extra):
        row = {
            "retailer": "pyaterochka",
            "product_id": "4250086",
            "name": "Тестовый товар",
            "price_min": 449.99,
            "price_max": 559.99,
            "category_slug": "discounts",
            "image_url": "https://example.com/image.jpg",
        }
        row.update(extra)
        return row

    def test_string_id_preserved(self):
        row = self.sample()
        doc = canonical_pyaterochka_document(row, 0, "pyaterochka_products.json", "a" * 64)
        self.assertEqual(doc["identity"]["external_id"], "4250086")
        self.assertEqual(doc["identity"]["source_id_raw"], "4250086")
        self.assertEqual(doc["retailer"], "5ka")
        self.assertEqual(doc["source_retailer_value"], "pyaterochka")
        self.assertEqual(doc["source_record_raw"], row)

    def test_price_min_and_max_remain_separate_unclassified_fields(self):
        row = self.sample()
        doc = canonical_pyaterochka_document(row, 0, "pyaterochka_products.json", "a" * 64)
        self.assertEqual(doc["price_fields"]["price_min"]["raw_value"], 449.99)
        self.assertEqual(doc["price_fields"]["price_max"]["raw_value"], 559.99)
        self.assertEqual(doc["price_fields"]["price_min"]["semantic_role"], "unclassified")
        self.assertEqual(doc["price_fields"]["price_max"]["semantic_role"], "unclassified")

    def test_category_slug_is_preserved_without_category_observation(self):
        row = self.sample()
        doc = canonical_pyaterochka_document(row, 0, "pyaterochka_products.json", "a" * 64)
        self.assertEqual(doc["categories"]["source_slug"], "discounts")
        self.assertIsNone(doc["categories"]["source_path"])
        self.assertIsNone(doc["categories"]["source_name"])

    def test_missing_price_fields_are_not_materialized(self):
        row = self.sample()
        del row["price_min"]
        del row["price_max"]
        doc = canonical_pyaterochka_document(row, 0, "pyaterochka_products.json", "a" * 64)
        self.assertEqual(doc["price_fields"], {})

    def test_null_and_value_states_are_distinct(self):
        row = self.sample(price_min=None)
        doc = canonical_pyaterochka_document(row, 0, "pyaterochka_products.json", "a" * 64)
        self.assertEqual(doc["price_fields"]["price_min"]["state"], "null")
        self.assertEqual(doc["price_fields"]["price_max"]["state"], "value")

    def test_boolean_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_pyaterochka_document(self.sample(product_id=True), 0, "x", "a" * 64)

    def test_numeric_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_pyaterochka_document(self.sample(product_id=4250086), 0, "x", "a" * 64)

    def test_empty_id_rejected(self):
        with self.assertRaises(ValueError):
            canonical_pyaterochka_document(self.sample(product_id=" "), 0, "x", "a" * 64)

    def test_wrong_retailer_rejected(self):
        with self.assertRaises(ValueError):
            canonical_pyaterochka_document(self.sample(retailer="other"), 0, "x", "a" * 64)

class PyaterochkaProfileTests(unittest.TestCase):
    def test_reviewed_snapshot_constants(self):
        self.assertEqual(EXPECTED_SOURCE_RECORDS, 5643)
        self.assertEqual(RETAILER, "5ka")
        self.assertEqual(SOURCE_RETAILER_VALUE, "pyaterochka")
        self.assertEqual(len(EXPECTED_SOURCE_SHA256), 64)

    def test_duplicate_ids_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "catalog.json"
            p.write_text(json.dumps([
                {"retailer":"pyaterochka","product_id":"1"},
                {"retailer":"pyaterochka","product_id":"1"},
            ]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                validate_pyaterochka_source(p, enforce_reviewed_profile=False)

    def test_wrong_retailer_detected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "catalog.json"
            p.write_text(json.dumps([{"retailer":"other","product_id":"1"}]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unexpected retailer"):
                validate_pyaterochka_source(p, enforce_reviewed_profile=False)

    def test_json_stream_rejects_non_object(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "catalog.json"
            p.write_text('[1]', encoding="utf-8")
            with self.assertRaises(ValueError):
                list(iter_json_array(p))

    def test_hash_mismatch_refused(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "catalog.json"
            p.write_text('[]', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_pyaterochka_source(p, expected_sha256="0"*64, enforce_reviewed_profile=False)

if __name__ == "__main__":
    unittest.main()
