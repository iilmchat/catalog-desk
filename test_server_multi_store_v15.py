"""Тесты S17: сервер принимает две сети и не смешивает пакетное обогащение."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("server_v15", ROOT / "catalog_ui_server_v14.py")
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)


class MultiStoreBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        keys = ("BASE_DIR", "DB_PATH", "EXPORT_DIR", "CATEGORY_DIR", "DETAIL_DIR", "PHOTO_DIR")
        self.old = {k: getattr(mod, k) for k in keys}
        mod.BASE_DIR = root
        mod.DB_PATH = root / "test.sqlite3"
        mod.EXPORT_DIR = root / "5ka_exports"
        mod.CATEGORY_DIR = root / "5ka_category_lists"
        mod.DETAIL_DIR = root / "5ka_product_details"
        mod.PHOTO_DIR = root / "photos5ka"
        for folder in (mod.EXPORT_DIR, mod.CATEGORY_DIR, mod.DETAIL_DIR, mod.PHOTO_DIR):
            folder.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(mod.DB_PATH)
        conn.executescript("""
        CREATE TABLE exports(
            file_name TEXT, category TEXT, sap_code TEXT, page_number INTEGER,
            product_rows INTEGER, unique_products INTEGER, captured_at TEXT,
            imported_at TEXT, source_url TEXT
        );
        CREATE TABLE products(
            sap_code TEXT NOT NULL, plu TEXT NOT NULL, name TEXT, categories TEXT,
            product_url TEXT, current_price REAL, regular_price REAL, promo_price REAL,
            discount_pct REAL, promo_label TEXT, image_url TEXT, image_file TEXT,
            first_seen_at TEXT, last_seen_at TEXT,
            first_imported_at TEXT, last_imported_at TEXT,
            PRIMARY KEY(sap_code,plu)
        );
        CREATE TABLE product_categories(sap_code TEXT, plu TEXT, category TEXT);
        CREATE TABLE price_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT DEFAULT '5ka', sap_code TEXT, plu TEXT,
            current_price REAL, regular_price REAL, promo_price REAL, discount_pct REAL,
            promo_label TEXT, category TEXT, product_name TEXT, image_url TEXT, product_url TEXT,
            captured_at TEXT, imported_at TEXT, source_hash TEXT, file_name TEXT, source_url TEXT
        );
        INSERT INTO products(sap_code,plu,name,categories,product_url,current_price,image_url,first_seen_at,last_seen_at,first_imported_at,last_imported_at)
        VALUES
          ('35XY','100001','Пятёрочка','Тесты','https://5ka.ru/product/test--100001/',100,NULL,'2026-11-24','2026-11-24','2026-11-24','2026-11-24'),
          ('XSEO','100001','Перекрёсток','Тесты','https://www.perekrestok.ru/product/test--100001',120,NULL,'2026-11-24','2026-11-24','2026-11-24','2026-11-24');
        """)
        conn.commit()
        conn.close()
        mod.ensure_ui_columns()
        conn = sqlite3.connect(mod.DB_PATH)
        conn.execute(
            "UPDATE products SET chain='perekrestok' WHERE sap_code='XSEO'"
        )
        conn.execute(
            "UPDATE enrichment_tasks SET chain='perekrestok' WHERE sap_code='XSEO'"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for key, value in self.old.items():
            setattr(mod, key, value)
        self.tmp.cleanup()

    def _payload(self):
        return {
            "schema_version": 2,
            "chain": "perekrestok",
            "source": "perekrestok.ru-visible-product-details-export",
            "captured_at": "2026-11-24T20:07:56.747Z",
            "source_url": "https://www.perekrestok.ru/product/test--100001",
            "store": {"sap_code": "XSEO"},
            "product": {
                "plu": "100001",
                "name": "Перекрёсток",
                "product_url": "https://www.perekrestok.ru/product/test--100001",
                "composition": "Сыры, масло сливочное",
                "nutrition": {"Белки": "14.5", "Жиры": "23.0"},
                "attributes": {"Бренд": "Тест"},
                "details_text": "Пищевая ценность на 100 г 284.0 Ккал 14.5 Белки 23.0 Жиры 4.1 Углеводы Состав Сыры, масло сливочное",
            },
        }

    def test_enrichment_status_is_isolated_by_chain(self):
        conn = sqlite3.connect(mod.DB_PATH)
        conn.execute(
            "UPDATE products SET image_url='https://example.test/main.jpg'"
        )
        conn.execute(
            "UPDATE enrichment_tasks SET status='pending' WHERE chain='5ka'"
        )
        conn.execute(
            "UPDATE enrichment_tasks SET status='opened' WHERE chain='perekrestok'"
        )
        conn.executemany(
            """INSERT INTO product_images(chain,sap_code,plu,image_url,local_file)
               VALUES(?,?,?,?,NULL)""",
            [
                ("5ka", "35XY", "100001", "https://example.test/5ka-gallery.jpg"),
                ("perekrestok", "XSEO", "100001", "https://example.test/pr-gallery.jpg"),
            ],
        )
        conn.execute(
            """INSERT INTO product_details(
                chain,sap_code,plu,product_name,composition,nutrition_json,
                attributes_json,details_text,first_imported_at,last_imported_at,
                last_captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "perekrestok", "XSEO", "100001", "Перекрёсток",
                "Сыры, масло сливочное", "{}", "{}",
                "Состав: сыры, масло сливочное",
                "2026-11-25", "2026-11-25", "2026-11-25",
            ),
        )
        conn.commit()
        conn.close()

        five = mod.enrichment_status({"chain": ["5ka"]})
        cross = mod.enrichment_status({"chain": ["perekrestok"]})

        self.assertEqual(five["chain"], "5ka")
        self.assertEqual(cross["chain"], "perekrestok")

        self.assertEqual(five["total_products"], 1)
        self.assertEqual(cross["total_products"], 1)
        self.assertEqual(five["main_photos_missing"], 1)
        self.assertEqual(cross["main_photos_missing"], 1)

        self.assertEqual(five["gallery_images_total"], 1)
        self.assertEqual(cross["gallery_images_total"], 1)
        self.assertEqual(five["gallery_images_missing"], 1)
        self.assertEqual(cross["gallery_images_missing"], 1)

        self.assertEqual(five["products_without_details"], 1)
        self.assertEqual(five["products_with_details"], 0)
        self.assertEqual(five["average_detail_percent"], 0.0)

        self.assertEqual(cross["products_without_details"], 0)
        self.assertEqual(cross["products_with_details"], 1)
        self.assertEqual(cross["average_detail_percent"], 40.0)

        self.assertEqual(five["task_counts"]["pending"], 1)
        self.assertEqual(five["task_counts"]["opened"], 0)
        self.assertEqual(cross["task_counts"]["pending"], 0)
        self.assertEqual(cross["task_counts"]["opened"], 1)

    def test_batch_selects_perekrestok_and_imports_under_its_chain(self):
        started = mod.batch_start({"chain": "perekrestok", "limit": 1, "delay_seconds": 12})
        self.assertEqual(started["chain"], "perekrestok")
        self.assertEqual(started["current"]["plu"], "100001")
        self.assertTrue(started["first_url"].startswith("https://www.perekrestok.ru/product/"))

        accepted = mod.batch_record(self._payload())
        self.assertTrue(accepted["accepted"])

        conn = sqlite3.connect(mod.DB_PATH)
        rows = conn.execute(
            "SELECT chain,sap_code,plu,composition FROM product_details"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0])[:3], ("perekrestok", "XSEO", "100001"))
        self.assertEqual(rows[0][3], "Сыры, масло сливочное")
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM product_details WHERE chain='5ka'"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def test_wrong_sap_for_chain_is_rejected(self):
        mod.batch_start({"chain": "perekrestok", "limit": 1, "delay_seconds": 12})
        payload = self._payload()
        payload["store"]["sap_code"] = "35XY"
        with self.assertRaises(ValueError):
            mod.batch_record(payload)

    def test_search_ingredients_and_compare_table(self):
        conn = sqlite3.connect(mod.DB_PATH)
        conn.execute("""
            INSERT OR REPLACE INTO product_details(chain,sap_code,plu,product_name,composition,
                nutrition_json,attributes_json,details_text,first_imported_at,last_imported_at,last_captured_at)
            VALUES('perekrestok','XSEO','100001','Перекрёсток','Сыры, пальмовое масло, сахар, арахис',
                '{"Белки":"20","Жиры":"3","Углеводы":"5","Ккал":"250"}',
                '{"Бренд":"Hochland","Вес":"0.15 кг","Срок хранения":"180 дн."}',
                'Состав Сыры, пальмовое масло, сахар, арахис',
                '2026-11-24','2026-11-24','2026-11-24')
        """)
        conn.commit()
        conn.close()

        found = mod.search_products({"q": ["белки больше 15 г, Composition содержит пальмовое масло"], "chain": ["perekrestok"]})
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["plu"], "100001")

        no_sugar = mod.search_products({"q": ["без сахара"], "chain": ["perekrestok"]})
        self.assertEqual(no_sugar["total"], 0)

        ingredients = mod.ingredients_data({"ingredient": ["пальмовое"], "chain": ["perekrestok"]})
        self.assertEqual(ingredients["total"], 1)
        self.assertIn("пальмовое масло", ingredients["items"][0]["markers"])
        self.assertIn("не лабораторная", ingredients["disclaimer"])

        table = mod.compare_table_data({"chain": ["perekrestok"], "plu": ["100001", "24139"]})
        self.assertEqual(len(table["items"]), 1)
        self.assertEqual(table["best"]["current_price"], 120)

    def test_automation_journal_and_api_v1(self):
        mod.record_operation_error("batch", "тестовая ошибка", chain="perekrestok")
        errors = mod.errors_data({"chain": ["perekrestok"]})
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["message"], "тестовая ошибка")
        self.assertEqual(mod.retry_error({"id": errors[0]["id"]})["resolved"], True)
        self.assertTrue(mod.errors_data({"chain": ["perekrestok"]})[0]["resolved"])

        scheduled = mod.set_schedule({"chain": ["perekrestok"], "run_date": ["2026-11-25"]})
        self.assertEqual(scheduled["status"], "planned_manual")
        self.assertFalse(scheduled["automatic_collection"])
        self.assertEqual(mod.schedule_data()["execution_mode"], "manual_dom_only")
        self.assertEqual(mod.schedule_data()["runs"][0]["chain"], "perekrestok")

        backup = mod.backup_now()
        self.assertTrue((Path(self.tmp.name) / backup["file"]).exists())

        self.assertEqual(mod.api_v1("/api/v1/stores", {})["success"], True)
        products = mod.api_v1("/api/v1/products", {"chain": ["perekrestok"]})
        self.assertEqual(products["data"]["total"], 1)
        self.assertEqual(
            mod.api_v1("/api/v1/products/perekrestok/100001", {})["data"]["product"]["plu"],
            "100001",
        )

    def test_batch_states_are_independent_for_two_stores(self):
        started_5ka = mod.batch_start({"chain": "5ka", "limit": 1, "delay_seconds": 12})
        started_pr = mod.batch_start({"chain": "perekrestok", "limit": 1, "delay_seconds": 12})
        self.assertEqual(started_5ka["chain"], "5ka")
        self.assertEqual(started_pr["chain"], "perekrestok")
        self.assertTrue(mod.batch_status("5ka")["active"])
        self.assertTrue(mod.batch_status("perekrestok")["active"])

        mod.batch_pause("Пауза Перекрёстка", "perekrestok")
        self.assertFalse(mod.batch_status("perekrestok")["active"])
        self.assertTrue(mod.batch_status("5ka")["active"])

    def test_stores_registry_and_cross_chain_compare(self):
        stores = mod.stores_data()
        self.assertEqual({s["chain"] for s in stores}, {"5ka", "perekrestok"})
        compared = mod.compare_data({"plu": ["100001"]})
        self.assertEqual(len(compared["items"]), 2)
        self.assertEqual(
            sorted(x["chain"] for x in compared["items"]), ["5ka", "perekrestok"]
        )


if __name__ == "__main__":
    unittest.main()
