# -*- coding: utf-8 -*-
"""Локальные тесты V11: доп. карточки, история, избранное и API-данные."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import catalog_ui_server_v12 as app


class CatalogV11Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "catalog.sqlite3"
        self.exports = self.root / "5ka_exports"
        self.details = self.root / "5ka_product_details"
        self.cats = self.root / "5ka_category_lists"
        self.photos = self.root / "photos5ka"
        for folder in (self.exports, self.details, self.cats, self.photos):
            folder.mkdir(parents=True, exist_ok=True)
        self.patches = [
            patch.object(app, "BASE_DIR", self.root),
            patch.object(app, "DB_PATH", self.db),
            patch.object(app, "EXPORT_DIR", self.exports),
            patch.object(app, "DETAIL_DIR", self.details),
            patch.object(app, "CATEGORY_DIR", self.cats),
            patch.object(app, "PHOTO_DIR", self.photos),
        ]
        for p in self.patches: p.start()
        self._create_v9_db()
        app.ensure_ui_columns()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def _create_v9_db(self):
        conn = sqlite3.connect(self.db)
        conn.executescript("""
        CREATE TABLE exports(source_hash TEXT PRIMARY KEY,file_name TEXT,category TEXT,source_url TEXT,sap_code TEXT,
            captured_at TEXT,imported_at TEXT,product_rows INTEGER,unique_products INTEGER,page_number TEXT);
        CREATE TABLE products(sap_code TEXT,plu TEXT,name TEXT,current_price REAL,regular_price REAL,promo_price REAL,
            discount_pct REAL,promo_label TEXT,categories TEXT,image_url TEXT,image_file TEXT,product_url TEXT,
            first_seen_at TEXT,last_seen_at TEXT,last_source_hash TEXT,first_imported_at TEXT,last_imported_at TEXT,
            PRIMARY KEY(sap_code,plu));
        CREATE TABLE product_categories(sap_code TEXT,plu TEXT,category TEXT,first_seen_at TEXT,last_seen_at TEXT,
            PRIMARY KEY(sap_code,plu,category));
        CREATE TABLE price_history(id INTEGER PRIMARY KEY AUTOINCREMENT,source_hash TEXT,sap_code TEXT,plu TEXT,category TEXT,
            captured_at TEXT,current_price REAL,regular_price REAL,promo_price REAL,discount_pct REAL,promo_label TEXT,
            raw_text TEXT,source_url TEXT,file_name TEXT,imported_at TEXT,product_name TEXT,image_url TEXT,product_url TEXT);
        INSERT INTO products VALUES('35XY','4334727','Аджика Апсны Абхазская 130г',299.99,299.99,NULL,NULL,NULL,
            'Овощи, фрукты, орехи','https://example.invalid/photo.jpg',NULL,
            'https://5ka.ru/product/adjika--4334727/','2026-11-24T10:00:00+00:00','2026-11-24T10:00:00+00:00',
            'hash','2026-11-24T10:01:00+00:00','2026-11-24T10:01:00+00:00');
        INSERT INTO product_categories VALUES('35XY','4334727','Овощи, фрукты, орехи','2026-11-24T10:00:00+00:00','2026-11-24T10:00:00+00:00');
        INSERT INTO exports VALUES('hash','catalog.json','Овощи, фрукты, орехи','https://5ka.ru/catalog/test/?page=1','35XY',
            '2026-11-24T10:00:00+00:00','2026-11-24T10:01:00+00:00',1,1,'1');
        INSERT INTO price_history(source_hash,sap_code,plu,category,captured_at,current_price,regular_price,promo_price,
            discount_pct,promo_label,raw_text,source_url,file_name,imported_at,product_name,image_url,product_url)
            VALUES('hash','35XY','4334727','Овощи, фрукты, орехи','2026-11-24T10:00:00+00:00',299.99,299.99,NULL,NULL,NULL,
            'Цена 299 99 ₽','https://5ka.ru/catalog/test/?page=1','catalog.json','2026-11-24T10:01:00+00:00',
            'Аджика Апсны Абхазская 130г','https://example.invalid/photo.jpg','https://5ka.ru/product/adjika--4334727/');
        """)
        conn.commit(); conn.close()

    def _detail_payload(self, captured="2026-11-24T11:00:00.000Z", composition="перец, вода, чеснок"):
        return {
            "schema_version": 1,
            "source": "5ka.ru-visible-product-details-export",
            "captured_at": captured,
            "source_url": "https://5ka.ru/product/adjika--4334727/",
            "store": {"sap_code": "35XY", "name": None},
            "product": {
                "plu": "4334727", "name": "Аджика Апсны Абхазская 130г",
                "product_url": "https://5ka.ru/product/adjika--4334727/",
                "image_url": "https://example.invalid/photo.jpg",
                "description": "Острая аджика",
                "composition": composition,
                "nutrition": {"Белки": "3.5", "Жиры": "1.8", "Углеводы": "6.9", "Ккал": "58"},
                "attributes": {"Вес": "0.13 кг", "Бренд": "Апсны", "Страна производства": "Абхазия",
                               "Срок хранения": "18 месяцев", "Условия хранения": "от 0°C до +25°C"},
                "details_text": "Состав\nперец, вода, чеснок\nВес\n0.13 кг",
                "image_gallery": [
                    {"url":"https://catalog-images.x5static.net/product/extra-1/800x800.jpeg","alt":"Вид спереди","position":1},
                    {"url":"https://catalog-images.x5static.net/product/extra-2/800x800.jpeg","alt":"Этикетка","position":2}
                ],
            },
        }

    def _upload_detail(self, payload, name="detail.json"):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return app.upload_payload({"files": [{"name": name, "content_base64": base64.b64encode(raw).decode("ascii")}]})

    def test_details_are_uploaded_and_imported(self):
        result = self._upload_detail(self._detail_payload())
        self.assertEqual(result["saved"][0]["kind"], "details")
        pending = app.pending_exports_data()
        self.assertTrue(any(x["kind"] == "details" for x in pending))
        counts = app.import_pending_details()
        self.assertEqual(counts[:3], (1, 1, 0))
        detail = app.product_detail({"sap": ["35XY"], "plu": ["4334727"]})["details"]
        self.assertEqual(detail["composition"], "перец, вода, чеснок")
        self.assertEqual(detail["nutrition"]["Ккал"], "58")
        self.assertEqual(detail["attributes"]["Бренд"], "Апсны")

    def test_duplicate_detail_file_does_not_duplicate_history(self):
        payload = self._detail_payload()
        self._upload_detail(payload)
        self.assertEqual(app.import_pending_details()[1], 1)
        # Upload the exact same content again: it is skipped by file hash.
        again = self._upload_detail(payload)
        self.assertEqual(len(again["skipped"]), 1)
        self.assertEqual(app.import_pending_details()[1], 0)
        conn = app.connect_db()
        try: count = conn.execute("SELECT COUNT(*) FROM product_detail_history").fetchone()[0]
        finally: conn.close()
        self.assertEqual(count, 1)

    def test_details_keep_snapshots_when_content_changes(self):
        self._upload_detail(self._detail_payload())
        app.import_pending_details()
        self._upload_detail(self._detail_payload("2026-11-25T11:00:00.000Z", "перец, вода, соль"), "detail_2.json")
        app.import_pending_details()
        detail = app.product_detail({"sap": ["35XY"], "plu": ["4334727"]})
        self.assertEqual(detail["details"]["composition"], "перец, вода, соль")
        self.assertEqual(len(detail["detail_history"]), 2)

    def test_gallery_images_are_imported_and_returned(self):
        self._upload_detail(self._detail_payload())
        counts = app.import_pending_details()
        self.assertEqual(counts[1], 1)
        detail = app.product_detail({"sap": ["35XY"], "plu": ["4334727"]})
        urls = [x["image_url"] for x in detail["images"]]
        self.assertIn("https://catalog-images.x5static.net/product/extra-1/800x800.jpeg", urls)
        self.assertIn("https://catalog-images.x5static.net/product/extra-2/800x800.jpeg", urls)
        self.assertGreaterEqual(len(urls), 2)

    def test_enrichment_queue_and_status(self):
        status = app.enrichment_status()
        self.assertEqual(status["total_products"], 1)
        self.assertEqual(status["products_without_details"], 1)
        queue = app.enrichment_queue({"q":["4334727"],"limit":["20"],"offset":["0"]})
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["items"][0]["plu"], "4334727")
        self._upload_detail(self._detail_payload())
        app.import_pending_details()
        status2 = app.enrichment_status()
        self.assertEqual(status2["products_with_details"], 1)
        self.assertEqual(status2["products_without_details"], 0)

    def test_favorites_are_persistent_and_filterable(self):
        app.set_favorite({"sap_code": "35XY", "plu": "4334727", "favorite": True})
        self.assertEqual(len(app.favorites_data()), 1)
        result = app.products_data({"q": [""], "category": [""], "promo": ["all"], "sort": ["name"],
                                    "page": ["1"], "limit": ["30"], "favorites": ["yes"]})
        self.assertEqual(result["total"], 1)
        app.set_favorite({"sap_code": "35XY", "plu": "4334727", "favorite": False})
        self.assertEqual(len(app.favorites_data()), 0)

    def test_analytics_and_detail_flags_are_available(self):
        self._upload_detail(self._detail_payload())
        app.import_pending_details()
        analytics = app.analytics_data()
        self.assertIn("daily", analytics)
        self.assertIn("top_discounts", analytics)
        product = app.products_data({"q": ["4334727"], "category": [""], "promo": ["all"], "sort": ["name"],
                                     "page": ["1"], "limit": ["30"]})["items"][0]
        self.assertTrue(product["has_details"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
