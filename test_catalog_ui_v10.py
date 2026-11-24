# -*- coding: utf-8 -*-
"""Offline tests for the local V10 UI API."""
import base64
import json
import tempfile
import unittest
from pathlib import Path

import catalog_ui_server_v10 as ui
from import_5ka_catalog_v9 import CatalogDB, load_json_rows, sha256_file


class CatalogUiV10Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db_path = root / "catalog.sqlite3"
        self.export_dir = root / "5ka_exports"
        self.category_dir = root / "5ka_category_lists"
        self.photo_dir = root / "photos5ka"
        self.export_dir.mkdir()
        self.category_dir.mkdir()
        self.photo_dir.mkdir()
        self.old = (ui.DB_PATH, ui.EXPORT_DIR, ui.CATEGORY_DIR, ui.PHOTO_DIR)
        ui.DB_PATH, ui.EXPORT_DIR, ui.CATEGORY_DIR, ui.PHOTO_DIR = self.db_path, self.export_dir, self.category_dir, self.photo_dir

        self.db = CatalogDB(self.db_path)
        row = {
            "SAP магазина": "35XY", "PLU": "777", "название": "Кофе тестовый",
            "цена сейчас, ₽": 100.0, "обычная цена, ₽": 125.0, "цена по акции, ₽": 100.0,
            "скидка, %": 20.0, "маркер акции": "-20%", "категории": "Напитки",
            "фото URL": "https://example.test/coffee.jpg", "карточка товара": "https://5ka.ru/product/coffee--777/",
            "страница каталога": "https://5ka.ru/catalog/drinks/?page=2", "время получения": "2026-11-24T10:00:00Z",
            "сырой текст карточки": "Старая цена 125 00 ₽ Цена 100 00 ₽",
        }
        self.db.import_rows("hash-one", "one.json", [row], {"category":"Напитки", "source_url":"https://5ka.ru/catalog/drinks/?page=2", "sap_code":"35XY", "captured_at":"2026-11-24T10:00:00Z"})
        row2 = dict(row, **{"цена сейчас, ₽": 90.0, "обычная цена, ₽": 125.0, "цена по акции, ₽": 90.0, "скидка, %": 28.0})
        self.db.import_rows("hash-two", "two.json", [row2], {"category":"Напитки", "source_url":"https://5ka.ru/catalog/drinks/?page=2", "sap_code":"35XY", "captured_at":"2026-11-25T10:00:00Z"})
        self.db.close()
        ui.ensure_ui_columns()

    def tearDown(self):
        ui.DB_PATH, ui.EXPORT_DIR, ui.CATEGORY_DIR, ui.PHOTO_DIR = self.old
        self.tmp.cleanup()

    def test_dashboard_and_products_query(self):
        d = ui.dashboard_data()
        self.assertEqual(d["products"], 1)
        self.assertEqual(d["observations"], 2)
        self.assertEqual(d["categories"], 1)
        products = ui.products_data({"q":["Кофе"], "limit":["10"], "page":["1"]})
        self.assertEqual(products["total"], 1)
        self.assertEqual(products["items"][0]["plu"], "777")

    def test_price_change_and_product_history(self):
        conn = ui.connect_db()
        try:
            changes = ui.get_price_changes(conn)
        finally:
            conn.close()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["delta"], -10.0)
        detail = ui.product_detail({"sap":["35XY"], "plu":["777"]})
        self.assertEqual(len(detail["history"]), 2)
        self.assertEqual(detail["product"]["current_price"], 90.0)

    def test_upload_visible_export_and_page_metadata(self):
        payload = {
            "source":"5ka.ru-visible-page-export", "captured_at":"2026-11-26T10:00:00Z",
            "source_url":"https://5ka.ru/catalog/drinks/?page=3", "category":"Напитки",
            "store":{"sap_code":"35XY"}, "pagination":{"current_page_number":3,"visible_page_numbers":[1,2,3,4],"max_page_linked":4},
            "products":[{"plu":"888","name":"Чай новый","product_url":"https://5ka.ru/product/tea--888/","image_url":"https://example.test/tea.jpg","raw_text":"Цена 55 00 ₽"}],
        }
        raw = json.dumps(payload, ensure_ascii=False).encode()
        res = ui.upload_payload({"files":[{"name":"tea.json","content_base64":base64.b64encode(raw).decode()}]})
        self.assertEqual(len(res["saved"]), 1)
        path = self.export_dir / "tea.json"
        rows, meta = load_json_rows(path)
        db = CatalogDB(self.db_path)
        db.import_rows(sha256_file(path), path.name, rows, meta)
        db.close()
        ui.persist_page_metadata()
        conn = ui.connect_db()
        try:
            r = conn.execute("SELECT page_number,max_page_linked,visible_page_numbers FROM exports WHERE file_name='tea.json'").fetchone()
            self.assertEqual(r["page_number"], "3")
            self.assertEqual(r["max_page_linked"], 4)
            self.assertEqual(json.loads(r["visible_page_numbers"]), [1,2,3,4])
        finally: conn.close()

    def test_category_manifest_is_stored_separately(self):
        payload = {"source":"5ka.ru-visible-category-links", "categories":[{"category_id":"A1","name":"Молочные продукты","url":"https://5ka.ru/catalog/milk/"}]}
        raw = json.dumps(payload, ensure_ascii=False).encode()
        res = ui.upload_payload({"files":[{"name":"cats.json","content_base64":base64.b64encode(raw).decode()}]})
        self.assertEqual(res["saved"][0]["kind"], "categories")
        conn = ui.connect_db()
        try:
            cats = ui.category_progress(conn)
        finally:
            conn.close()
        milk = next(x for x in cats if x["category"] == "Молочные продукты")
        self.assertEqual(milk["files"], 0)
        self.assertEqual(milk["status"], "Не начато")


if __name__ == "__main__":
    unittest.main(verbosity=2)
