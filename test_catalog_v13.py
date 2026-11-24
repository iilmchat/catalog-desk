"""Tests for V13 mass-enrichment queue and safe V9 data migration."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog_ui_server_v13 as app


class MassEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "catalog.sqlite3"
        self.details = self.root / "5ka_product_details"
        self.details.mkdir()
        self.patches = [
            patch.object(app, "BASE_DIR", self.root),
            patch.object(app, "DB_PATH", self.db),
            patch.object(app, "DETAIL_DIR", self.details),
            patch.object(app, "PHOTO_DIR", self.root / "photos5ka"),
        ]
        for p in self.patches: p.start()
        self._create_db()
        app.ensure_ui_columns()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def _create_db(self):
        c = sqlite3.connect(self.db)
        c.executescript("""
        CREATE TABLE exports(source_hash TEXT PRIMARY KEY,file_name TEXT,category TEXT,source_url TEXT,sap_code TEXT,
            captured_at TEXT,imported_at TEXT,product_rows INTEGER,unique_products INTEGER,page_number TEXT,
            max_page_linked INTEGER,visible_page_numbers TEXT);
        CREATE TABLE products(sap_code TEXT,plu TEXT,name TEXT,current_price REAL,regular_price REAL,promo_price REAL,
            discount_pct REAL,promo_label TEXT,categories TEXT,image_url TEXT,image_file TEXT,product_url TEXT,
            first_seen_at TEXT,last_seen_at TEXT,last_source_hash TEXT,first_imported_at TEXT,last_imported_at TEXT,
            PRIMARY KEY(sap_code,plu));
        CREATE TABLE product_categories(sap_code TEXT,plu TEXT,category TEXT,first_seen_at TEXT,last_seen_at TEXT,
            PRIMARY KEY(sap_code,plu,category));
        CREATE TABLE price_history(id INTEGER PRIMARY KEY AUTOINCREMENT,source_hash TEXT,sap_code TEXT,plu TEXT,category TEXT,
            captured_at TEXT,current_price REAL,regular_price REAL,promo_price REAL,discount_pct REAL,promo_label TEXT,
            raw_text TEXT,source_url TEXT,file_name TEXT,imported_at TEXT,product_name TEXT,image_url TEXT,product_url TEXT);
        INSERT INTO products VALUES
          ('35XY','111','Товар без подробностей',99,99,NULL,NULL,NULL,'Овощи','https://catalog-images.x5static.net/a.jpg',NULL,'https://5ka.ru/product/a--111/','2026-11-24T10:00:00Z','2026-11-24T10:00:00Z','h1','2026-11-24T10:01:00Z','2026-11-24T10:01:00Z'),
          ('35XY','222','Товар с подробностями',149,149,NULL,NULL,NULL,'Молочные продукты','https://catalog-images.x5static.net/b.jpg',NULL,'https://5ka.ru/product/b--222/','2026-11-24T10:00:00Z','2026-11-24T10:00:00Z','h2','2026-11-24T10:01:00Z','2026-11-24T10:01:00Z');
        INSERT INTO product_categories VALUES
          ('35XY','111','Овощи','2026-11-24T10:00:00Z','2026-11-24T10:00:00Z'),
          ('35XY','222','Молочные продукты','2026-11-24T10:00:00Z','2026-11-24T10:00:00Z');
        """)
        c.commit(); c.close()

    def _write_detail(self):
        payload = {
            "schema_version": 1,
            "source": "5ka.ru-visible-product-details-export",
            "captured_at": "2026-11-24T12:00:00Z",
            "store": {"sap_code": "35XY"},
            "product": {"plu": "111", "name": "Товар без подробностей",
                        "composition": "вода, овощи", "description": "Описание",
                        "nutrition": {"protein": "2 г"},
                        "attributes": {"Бренд": "Тест"},
                        "details_text": "Состав и характеристики"}
        }
        (self.details / "detail_111.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_migration_creates_one_pending_task_per_product(self):
        c = app.connect_db()
        try:
            count = c.execute("SELECT COUNT(*) FROM enrichment_tasks").fetchone()[0]
        finally: c.close()
        self.assertEqual(count, 2)

    def test_existing_details_are_marked_processed_on_migration(self):
        c = app.connect_db()
        try:
            c.execute("DELETE FROM enrichment_tasks WHERE plu='222'")
            c.execute("""INSERT INTO product_details(sap_code,plu,composition,nutrition_json,attributes_json,
                       details_text,first_imported_at,last_imported_at,last_captured_at)
                       VALUES('35XY','222','состав','{}','{}','описание','t','t','t')""")
            c.commit()
        finally: c.close()
        app.ensure_ui_columns()
        c = app.connect_db()
        try:
            self.assertEqual(c.execute("SELECT status FROM enrichment_tasks WHERE plu='222'").fetchone()[0], "processed")
        finally: c.close()

    def test_queue_prioritizes_pending_and_filters_category(self):
        d = app.enrichment_queue({"status": ["pending"], "completeness": ["none"], "limit": ["25"], "offset": ["0"]})
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["items"][0]["plu"], "111")
        self.assertIn("Овощи", d["categories"])
        cat = app.enrichment_queue({"category": ["Молочные продукты"], "status": ["all"], "completeness": ["all"]})
        self.assertEqual([x["plu"] for x in cat["items"]], ["222"])

    def test_task_state_transitions(self):
        app.update_enrichment_task({"sap_code": "35XY", "plu": "111", "action": "opened"})
        app.update_enrichment_task({"sap_code": "35XY", "plu": "111", "action": "exported"})
        c = app.connect_db()
        try:
            self.assertEqual(c.execute("SELECT status FROM enrichment_tasks WHERE plu='111'").fetchone()[0], "exported")
        finally: c.close()
        app.update_enrichment_task({"sap_code": "35XY", "plu": "111", "action": "skip"})
        app.update_enrichment_task({"sap_code": "35XY", "plu": "111", "action": "reset"})
        c = app.connect_db()
        try:
            self.assertEqual(c.execute("SELECT status FROM enrichment_tasks WHERE plu='111'").fetchone()[0], "pending")
            self.assertGreaterEqual(c.execute("SELECT COUNT(*) FROM enrichment_task_events WHERE plu='111'").fetchone()[0], 4)
        finally: c.close()

    def test_detail_import_marks_card_processed(self):
        self._write_detail()
        files, products, skipped, errors = app.import_pending_details()
        self.assertEqual((files, products, skipped), (1, 1, 0))
        self.assertEqual(errors, [])
        c = app.connect_db()
        try:
            self.assertEqual(c.execute("SELECT status FROM enrichment_tasks WHERE plu='111'").fetchone()[0], "processed")
            row = c.execute("SELECT composition,nutrition_json,attributes_json FROM product_details WHERE plu='111'").fetchone()
            self.assertIn("вода", row[0])
            self.assertIn("protein", row[1])
            self.assertIn("Бренд", row[2])
        finally: c.close()

    def test_summary_reflects_processed_count(self):
        self._write_detail(); app.import_pending_details()
        data = app.enrichment_queue({"status": ["all"], "completeness": ["all"]})
        self.assertEqual(data["summary"]["processed"], 1)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["category_progress"][0]["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
