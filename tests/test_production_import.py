from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from s24.schema_v1 import apply_schema
from s24.storage_v1 import database_counts, ingest_one_source_bulk
from tools import (
    s24_auchan_pilot,
    s24_globus_pilot,
    s24_dixy_pilot,
    s24_chizhik_pilot,
    s24_perekrestok_pilot,
    s24_pyaterochka_pilot,
)


class ProductionImportPackageTests(unittest.TestCase):
    def test_reviewed_aggregate_totals(self):
        expected = {
            "source_records": 159758,
            "confirmed_products": 152874,
            "pending_observations": 6884,
            "identity_candidates": 6593,
            "price_observations": 767683,
            "category_observations": 324760,
            "asset_observations": 159758,
        }
        actual = {
            "source_records": sum(x["records"] for x in s24_production_expected().values()),
            "confirmed_products": sum(x["resolved"] for x in s24_production_expected().values()),
            "pending_observations": sum(x["pending"] for x in s24_production_expected().values()),
            "identity_candidates": sum(x["candidates"] for x in s24_production_expected().values()),
            "price_observations": sum(x["prices"] for x in s24_production_expected().values()),
            "category_observations": sum(x["categories"] for x in s24_production_expected().values()),
            "asset_observations": sum(x["assets"] for x in s24_production_expected().values()),
        }
        self.assertEqual(actual, expected)

    def test_all_six_mappers_share_storage_and_import_in_one_transaction(self):
        mappers = [
            ("auchan", "auchan_products.json", s24_auchan_pilot,
             [{"retailer": "auchan", "product_id": 101, "name": "A", "price": 10,
               "category_path": ["Food"], "url": "https://auchan.test/a", "image_url": "https://img.test/a.jpg"}]),
            ("globus", "globus_products.json", s24_globus_pilot,
             [{"retailer": "globus", "product_id": 201, "name": "G", "price": 11,
               "category_path": ["Food"], "url": "https://globus.test/g", "image_url": "https://img.test/g.jpg"}]),
            ("dixy", "dixy_products.json", s24_dixy_pilot,
             [{"retailer": "dixy", "product_id": None, "name": "D", "price_min": 12,
               "url": "https://dixy.ru/product/item-1234567890", "image_url": "https://img.test/d.jpg"}]),
            ("chizhik", "chizhik_products.json", s24_chizhik_pilot,
             [{"retailer": "chizhik", "product_id": 301, "name": "C", "price": 13,
               "category_path": ["Food"], "url": "https://chizhik.test/c", "image_url": "https://img.test/c.jpg"}]),
            ("perekrestok", "perekrestok_products.json", s24_perekrestok_pilot,
             [{"retailer": "perekrestok", "product_id": "401", "name": "P", "price_min": 14,
               "price_max": 15, "category_slug": "food", "url": "https://perekrestok.test/p",
               "image_url": "https://img.test/p.jpg"}]),
            ("5ka", "pyaterochka_products.json", s24_pyaterochka_pilot,
             [{"retailer": "pyaterochka", "product_id": "501", "name": "5", "price_min": 16,
               "price_max": 17, "category_slug": "food", "url": "https://5ka.test/5",
               "image_url": "https://img.test/5.jpg"}]),
        ]
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        apply_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for retailer, file_name, module, rows in mappers:
                    source_path = root / file_name
                    source_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
                    result = ingest_one_source_bulk(
                        conn,
                        retailer=retailer,
                        source_file_name=file_name,
                        source_sha256=source_sha,
                        expected_count=1,
                        jsonl_lines=module.canonical_lines(source_path, file_name, source_sha),
                        batch_size=1,
                    )
                    self.assertEqual(result["inserted_observations"], 1)
                    repeat = ingest_one_source_bulk(
                        conn, retailer=retailer, source_file_name=file_name,
                        source_sha256=source_sha, expected_count=1, jsonl_lines=(), batch_size=1,
                    )
                    self.assertTrue(repeat["skipped_existing_import"])
                    self.assertEqual(repeat["inserted_observations"], 0)
            conn.commit()
            counts = database_counts(conn)
            self.assertEqual(counts["retailer_catalog_imports"], 6)
            self.assertEqual(counts["retailer_product_observations"], 6)
            self.assertEqual(counts["retailer_catalog_products"], 5)
            self.assertEqual(counts["retailer_product_identity_candidates"], 1)
            self.assertEqual(counts["retailer_product_asset_observations"], 6)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM retailer_product_observations WHERE retailer_key='dixy' AND identity_status='pending'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT candidate_status FROM retailer_product_identity_candidates"
            ).fetchone()[0], "unverified")
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(len(conn.execute("PRAGMA foreign_key_check").fetchall()), 0)
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def test_transaction_rollback_leaves_no_partial_import(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        apply_schema(conn)
        conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Simulate a failure after the first source has started writing.
            conn.execute(
                "INSERT INTO retailer_catalog_imports "
                "(retailer_key,source_file_name,source_sha256,imported_at_utc,source_record_count) "
                "VALUES('auchan','auchan_products.json',?, '2026-11-27T00:00:00+00:00', 1)",
                ("a" * 64,),
            )
            raise RuntimeError("injected failure")
        except RuntimeError:
            conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM retailer_catalog_imports").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM retailer_product_observations").fetchone()[0], 0)
        conn.close()


def s24_production_expected():
    return {
        "auchan": {"records": 81413, "resolved": 81413, "pending": 0, "candidates": 0, "prices": 407065, "categories": 238372, "assets": 81413},
        "globus": {"records": 48158, "resolved": 48158, "pending": 0, "candidates": 0, "prices": 288948, "categories": 67453, "assets": 48158},
        "dixy": {"records": 15311, "resolved": 8427, "pending": 6884, "candidates": 6593, "prices": 45900, "categories": 15278, "assets": 15311},
        "chizhik": {"records": 3231, "resolved": 3231, "pending": 0, "candidates": 0, "prices": 6462, "categories": 3657, "assets": 3231},
        "perekrestok": {"records": 6002, "resolved": 6002, "pending": 0, "candidates": 0, "prices": 8022, "categories": 0, "assets": 6002},
        "pyaterochka": {"records": 5643, "resolved": 5643, "pending": 0, "candidates": 0, "prices": 11286, "categories": 0, "assets": 5643},
    }


if __name__ == "__main__":
    unittest.main()

class ProductionApplyFlowTests(unittest.TestCase):
    def test_preflight_report_to_atomic_apply_flow_on_disposable_database(self):
        from unittest.mock import patch
        import hashlib
        import shutil
        import tools.s24_production_import as production

        mappers = [
            ("auchan", "auchan_products.json", s24_auchan_pilot,
             [{"retailer": "auchan", "product_id": 101, "price": 10,
               "category_path": ["Food"], "url": "https://auchan.test/a", "image_url": "https://img.test/a.jpg"}],
             {"price": 1}, 1, 1, 0, 0),
            ("globus", "globus_products.json", s24_globus_pilot,
             [{"retailer": "globus", "product_id": 201, "price": 11,
               "category_path": ["Food"], "url": "https://globus.test/g", "image_url": "https://img.test/g.jpg"}],
             {"price": 1}, 1, 1, 0, 0),
            ("dixy", "dixy_products.json", s24_dixy_pilot,
             [{"retailer": "dixy", "product_id": None, "price_min": 12,
               "url": "https://dixy.ru/product/item-1234567890", "image_url": "https://img.test/d.jpg"}],
             {"price_min": 1}, 0, 0, 1, 1),
            ("chizhik", "chizhik_products.json", s24_chizhik_pilot,
             [{"retailer": "chizhik", "product_id": 301, "price": 13,
               "category_path": ["Food"], "url": "https://chizhik.test/c", "image_url": "https://img.test/c.jpg"}],
             {"price": 1}, 1, 1, 0, 0),
            ("perekrestok", "perekrestok_products.json", s24_perekrestok_pilot,
             [{"retailer": "perekrestok", "product_id": "401", "price_min": 14, "price_max": 15,
               "category_slug": "food", "url": "https://perekrestok.test/p", "image_url": "https://img.test/p.jpg"}],
             {"price_min": 1, "price_max": 1}, 1, 0, 0, 0),
            ("pyaterochka", "pyaterochka_products.json", s24_pyaterochka_pilot,
             [{"retailer": "pyaterochka", "product_id": "501", "price_min": 16, "price_max": 17,
               "category_slug": "food", "url": "https://5ka.test/5", "image_url": "https://img.test/5.jpg"}],
             {"price_min": 1, "price_max": 1}, 1, 0, 0, 0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "sources"
            source_dir.mkdir()
            db_path = root / "working.sqlite3"
            backup_path = root / "backup.sqlite3"
            preflight_path = root / "preflight.json"
            report_path = root / "final_report.json"

            # Create an isolated S24 database with schema v1 and no source data.
            db = sqlite3.connect(db_path)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            apply_schema(db)
            db.commit()
            initial_state = production._database_state(db)
            db.close()

            # Create the SQLite Backup-API copy that the apply phase requires.
            source = sqlite3.connect(db_path)
            target = sqlite3.connect(backup_path)
            source.backup(target)
            source.close()
            target.close()

            source_specs = []
            source_profiles = []
            expected_outcomes = {}
            for slug, file_name, module, rows, price_fields, resolved, cats, pending, candidates in mappers:
                path = source_dir / file_name
                path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                sha = hashlib.sha256(path.read_bytes()).hexdigest()
                state = module.source_file_state(path)
                source_spec = {
                    "slug": slug,
                    "retailer_key": "5ka" if slug == "pyaterochka" else slug,
                    "file": file_name,
                    "module": f"tools.s24_{slug if slug != 'pyaterochka' else 'pyaterochka'}_pilot",
                    "validator": f"validate_{slug if slug != 'pyaterochka' else 'pyaterochka'}_source",
                    "module_obj": module,
                    "expected_sha256": sha,
                    "expected_count": 1,
                }
                source_specs.append(source_spec)
                profile = {
                    "source_file": file_name,
                    "source_path": str(path),
                    "source_sha256": sha,
                    "source_size_bytes": state["size_bytes"],
                    "source_record_count": 1,
                    "unique_external_ids": resolved,
                    "resolved_identity_records": resolved,
                    "pending_identity_records": pending,
                    "invalid_identity_records": 0,
                    "url_identity_candidate_records": candidates,
                    "expected_price_observation_count": sum(price_fields.values()),
                    "price_field_presence_counts": price_fields,
                    "expected_category_observation_count": cats,
                    "expected_asset_observation_count": 1,
                    "image_url_classifications": {"image_candidate": 1},
                }
                source_profiles.append({
                    "slug": slug,
                    "retailer_key": source_spec["retailer_key"],
                    "file": file_name,
                    "module": source_spec["module"],
                    "validator": source_spec["validator"],
                    "expected_sha256": sha,
                    "expected_count": 1,
                    "profile": profile,
                })
                expected_outcomes[slug] = {
                    "records": 1, "resolved": resolved, "pending": pending,
                    "candidates": candidates, "prices": sum(price_fields.values()),
                    "categories": cats, "assets": 1,
                }

            backup_conn = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
            backup_conn.row_factory = sqlite3.Row
            backup_state = production._database_state(backup_conn)
            backup_conn.close()
            preflight = {
                "contract": "catalog-desk.s24-production-import-preflight",
                "preflight_passed": True,
                "production_database": str(db_path.resolve()),
                "source_profiles": source_profiles,
                "database_state_before": initial_state,
                "backup": {
                    "path": str(backup_path),
                    "sha256": hashlib.sha256(backup_path.read_bytes()).hexdigest(),
                },
            }
            preflight_path.write_text(json.dumps(preflight), encoding="utf-8")

            with patch.object(production, "load_source_modules", return_value=source_specs), \
                 patch.object(production, "validate_sources", return_value=source_profiles), \
                 patch.object(production, "EXPECTED_SOURCE_OUTCOMES", expected_outcomes):
                report = production.run_apply(db_path, source_dir, preflight_path, report_path)

            self.assertTrue(report["production_import_committed"])
            self.assertTrue(report["production_database_modified"])
            self.assertTrue(report["all_six_sources_committed"])
            self.assertTrue(report["idempotency_noop_confirmed_for_each_source"])
            self.assertEqual(report["verification_after_commit"]["totals"]["retailer_catalog_imports"], 6)
            self.assertEqual(report["verification_after_commit"]["totals"]["retailer_product_observations"], 6)
            self.assertEqual(report["verification_after_commit"]["totals"]["retailer_catalog_products"], 5)
            self.assertEqual(report["verification_after_commit"]["totals"]["retailer_product_identity_candidates"], 1)
            self.assertEqual(report["verification_after_commit"]["totals"]["retailer_product_asset_observations"], 6)
            self.assertTrue(report_path.is_file())

            # A second apply with the same preflight/source snapshot must be a no-op.
            noop_report_path = root / "repeat_report.json"
            with patch.object(production, "load_source_modules", return_value=source_specs), \
                 patch.object(production, "validate_sources", return_value=source_profiles), \
                 patch.object(production, "EXPECTED_SOURCE_OUTCOMES", expected_outcomes):
                noop_report = production.run_apply(db_path, source_dir, preflight_path, noop_report_path)
            self.assertTrue(noop_report["production_import_committed"])
            self.assertFalse(noop_report["production_database_modified"])
            self.assertTrue(noop_report["idempotency_noop_confirmed_for_each_source"])
