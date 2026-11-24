# -*- coding: utf-8 -*-
"""Offline tests for Pyaterochka Catalog V9. No website or photo requests."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from import_5ka_catalog_v9 import CatalogDB, bootstrap_v9_database


class CatalogV9Tests(unittest.TestCase):
    def make_row(self, price, *, name="Товар тестовый", regular=120.0, discount=16.7, image="https://example.test/a.jpg"):
        return {
            "SAP магазина": "35XY", "PLU": "123", "название": name,
            "цена сейчас, ₽": price, "обычная цена, ₽": regular,
            "цена по акции, ₽": price if discount is not None else None,
            "скидка, %": discount, "маркер акции": "-17%" if discount is not None else None,
            "категории": "Категория А", "фото URL": image,
            "карточка товара": "https://5ka.ru/product/test--123/",
            "время получения": None, "сырой текст карточки": "Тестовая карточка",
        }

    def test_first_seen_and_first_import_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDB(Path(tmp) / "catalog.sqlite3")
            row = self.make_row(100.0)
            meta = {"category": "Категория А", "source_url": "https://5ka.ru/catalog/a/?page=1",
                    "sap_code": "35XY", "captured_at": "2026-11-24T10:00:00Z"}
            db.import_rows("hash-1", "a.json", [row], meta)
            product = db.products()[0]
            self.assertEqual(product["дата первого появления (UTC)"], "2026-11-24T10:00:00+00:00")
            self.assertRegex(product["дата первого импорта (UTC)"], r"^20\d\d-\d\d-\d\dT")
            self.assertEqual(product["дата первого импорта (UTC)"], product["дата последнего импорта (UTC)"])
            history = db.history()[0]
            self.assertRegex(history["дата импорта (UTC)"], r"^20\d\d-\d\d-\d\dT")
            self.assertEqual(history["название на момент выгрузки"], "Товар тестовый")
            db.close()

    def test_two_snapshots_track_price_and_history_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDB(Path(tmp) / "catalog.sqlite3")
            row1 = self.make_row(100.0)
            meta1 = {"category": "Категория А", "source_url": "https://5ka.ru/catalog/a/?page=1",
                     "sap_code": "35XY", "captured_at": "2026-11-24T10:00:00Z"}
            db.import_rows("hash-1", "a.json", [row1], meta1)
            row2 = self.make_row(90.0, name="Товар тестовый обновлён", regular=120.0, discount=25.0,
                                 image="https://example.test/b.jpg")
            meta2 = {"category": "Категория А", "source_url": "https://5ka.ru/catalog/a/?page=1",
                     "sap_code": "35XY", "captured_at": "2026-11-25T10:00:00Z"}
            db.import_rows("hash-2", "b.json", [row2], meta2)

            products = db.products()
            self.assertEqual(len(products), 1)
            self.assertEqual(products[0]["цена сейчас, ₽"], 90.0)
            self.assertEqual(products[0]["наблюдений цены"], 2)
            self.assertEqual(products[0]["дата первого появления (UTC)"], "2026-11-24T10:00:00+00:00")
            self.assertEqual(products[0]["дата последнего наблюдения (UTC)"], "2026-11-25T10:00:00+00:00")

            hist = db.history()
            self.assertEqual(len(hist), 2)
            self.assertEqual(hist[0]["название на момент выгрузки"], "Товар тестовый")
            self.assertEqual(hist[1]["название на момент выгрузки"], "Товар тестовый обновлён")
            self.assertNotEqual(hist[0]["дата импорта (UTC)"], hist[1]["дата импорта (UTC)"])

            changes = db.changes()
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["предыдущая цена, ₽"], 100.0)
            self.assertEqual(changes[0]["новая цена, ₽"], 90.0)
            self.assertEqual(changes[0]["изменение, ₽"], -10.0)
            self.assertIn("текущая цена", changes[0]["что изменилось"])

            events = db.events()
            self.assertEqual(events[0]["тип события"], "Первое появление")
            self.assertEqual(events[1]["тип события"], "Изменение карточки/цены")
            self.assertIn("название", events[1]["изменения"])
            self.assertIn("фото", events[1]["изменения"])
            db.close()

    def test_promo_removal_is_recorded_not_carried_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDB(Path(tmp) / "catalog.sqlite3")
            discounted = self.make_row(90.0, regular=120.0, discount=25.0)
            meta1 = {"category": "Категория А", "sap_code": "35XY", "captured_at": "2026-11-24T10:00:00Z"}
            db.import_rows("promo-1", "promo.json", [discounted], meta1)
            no_promo = self.make_row(120.0, regular=120.0, discount=None)
            no_promo["цена по акции, ₽"] = None
            no_promo["маркер акции"] = None
            meta2 = {"category": "Категория А", "sap_code": "35XY", "captured_at": "2026-11-25T10:00:00Z"}
            db.import_rows("promo-2", "regular.json", [no_promo], meta2)
            product = db.products()[0]
            self.assertIsNone(product["цена по акции, ₽"])
            self.assertIsNone(product["скидка, %"])
            changes = db.changes()
            self.assertEqual(len(changes), 1)
            self.assertIn("цена по акции", changes[0]["что изменилось"])
            self.assertIn("процент скидки", changes[0]["что изменилось"])
            db.close()

    def test_same_file_is_not_imported_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDB(Path(tmp) / "catalog.sqlite3")
            row = self.make_row(100.0)
            meta = {"category": "Категория А", "sap_code": "35XY", "captured_at": "2026-11-24T10:00:00Z"}
            self.assertEqual(db.import_rows("same-hash", "a.json", [row], meta), (1, 1))
            self.assertEqual(db.import_rows("same-hash", "a.json", [row], meta), (0, 0))
            self.assertEqual(len(db.history()), 1)
            db.close()

    def test_migrates_v8_import_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            v8 = Path(tmp) / "v8.sqlite3"
            conn = sqlite3.connect(v8)
            conn.executescript("""
            CREATE TABLE exports(source_hash TEXT PRIMARY KEY,file_name TEXT NOT NULL,category TEXT,source_url TEXT,sap_code TEXT,captured_at TEXT NOT NULL,imported_at TEXT NOT NULL,product_rows INTEGER NOT NULL DEFAULT 0,unique_products INTEGER NOT NULL DEFAULT 0,page_number TEXT);
            CREATE TABLE products(sap_code TEXT NOT NULL,plu TEXT NOT NULL,name TEXT,current_price REAL,regular_price REAL,promo_price REAL,discount_pct REAL,promo_label TEXT,categories TEXT,image_url TEXT,image_file TEXT,product_url TEXT,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,last_source_hash TEXT,PRIMARY KEY(sap_code,plu));
            CREATE TABLE product_categories(sap_code TEXT NOT NULL,plu TEXT NOT NULL,category TEXT NOT NULL,first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,PRIMARY KEY(sap_code,plu,category));
            CREATE TABLE price_history(id INTEGER PRIMARY KEY AUTOINCREMENT,source_hash TEXT NOT NULL,sap_code TEXT NOT NULL,plu TEXT NOT NULL,category TEXT,captured_at TEXT NOT NULL,current_price REAL,regular_price REAL,promo_price REAL,discount_pct REAL,promo_label TEXT,raw_text TEXT,source_url TEXT,file_name TEXT NOT NULL,UNIQUE(source_hash,sap_code,plu));
            INSERT INTO exports VALUES('h1','one.json','Категория А','https://5ka.ru/catalog/a/','35XY','2026-11-24T10:00:00+00:00','2026-11-24T10:05:00+00:00',1,1,'1');
            INSERT INTO products VALUES('35XY','123','Старое имя',100,120,100,16.7,'-17%','Категория А','https://example.test/old.jpg',NULL,'https://5ka.ru/product/test--123/','2026-11-24T10:00:00+00:00','2026-11-24T10:00:00+00:00','h1');
            INSERT INTO product_categories VALUES('35XY','123','Категория А','2026-11-24T10:00:00+00:00','2026-11-24T10:00:00+00:00');
            INSERT INTO price_history(source_hash,sap_code,plu,category,captured_at,current_price,regular_price,promo_price,discount_pct,promo_label,raw_text,source_url,file_name) VALUES('h1','35XY','123','Категория А','2026-11-24T10:00:00+00:00',100,120,100,16.7,'-17%','raw','https://5ka.ru/catalog/a/','one.json');
            """)
            conn.commit()
            conn.close()
            db = CatalogDB(v8)
            product = db.products()[0]
            hist = db.history()[0]
            self.assertEqual(product["дата первого импорта (UTC)"], "2026-11-24T10:05:00+00:00")
            self.assertEqual(hist["дата импорта (UTC)"], "2026-11-24T10:05:00+00:00")
            self.assertEqual(hist["название на момент выгрузки"], "Старое имя")
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
