# -*- coding: utf-8 -*-
"""Локальные тесты v8. Не обращаются в интернет и не скачивают фото."""
import json
import tempfile
import unittest
from pathlib import Path

from import_5ka_catalog_v8 import CatalogDB, parse_visible_card, row_from_visible_export, to_price


class ParserTests(unittest.TestCase):
    def test_price_parser_with_russian_spaces(self):
        self.assertEqual(to_price("1 359 00 ₽"), 1359.00)
        self.assertEqual(to_price("417 99"), 417.99)
        self.assertEqual(to_price("1 249,50"), 1249.50)

    def test_discount_card(self):
        current, regular, promo, label, pct = parse_visible_card(
            "-28% Колбаса тестовая 350 г Старая цена 599 99 ₽ Цена 434 99 ₽"
        )
        self.assertEqual(current, 434.99)
        self.assertEqual(regular, 599.99)
        self.assertEqual(promo, 434.99)
        self.assertAlmostEqual(pct, 27.5, places=1)
        self.assertEqual(label, "-28%")

    def test_multi_buy_marker_is_not_fabricated_as_discount_percent(self):
        current, regular, promo, label, pct = parse_visible_card(
            "% от 2 шт Сосиски тестовые 450 г Цена 269 99 ₽"
        )
        self.assertEqual(current, 269.99)
        self.assertIsNone(regular)
        self.assertIsNone(promo)
        self.assertEqual(label, "% от 2 шт")
        self.assertIsNone(pct)

    def test_database_deduplicates_same_export_and_keeps_two_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDB(Path(tmp) / "test.sqlite3")
            row1 = {"SAP магазина":"35XY","PLU":"123","название":"Товар","цена сейчас, ₽":100.0,
                    "обычная цена, ₽":120.0,"цена по акции, ₽":100.0,"скидка, %":16.7,
                    "маркер акции":"-17%","категории":"Категория А","фото URL":"https://example.test/1.jpg",
                    "карточка товара":"https://5ka.ru/product/test--123/","время получения":"2026-11-24T10:00:00+00:00"}
            meta1 = {"category":"Категория А","source_url":"https://5ka.ru/catalog/a/?page=1","sap_code":"35XY","captured_at":"2026-11-24T10:00:00Z"}
            self.assertEqual(db.import_rows("hash-1","a.json",[row1],meta1),(1,1))
            self.assertTrue(db.has_export("hash-1"))
            self.assertEqual(db.import_rows("hash-1","a.json",[row1],meta1),(0,0))
            row2 = dict(row1)
            row2["цена сейчас, ₽"] = 90.0
            row2["цена по акции, ₽"] = 90.0
            row2["скидка, %"] = 25.0
            row2["время получения"] = "2026-11-25T10:00:00+00:00"
            meta2 = {"category":"Категория А","source_url":"https://5ka.ru/catalog/a/?page=1","sap_code":"35XY","captured_at":"2026-11-25T10:00:00Z"}
            self.assertEqual(db.import_rows("hash-2","b.json",[row2],meta2),(1,1))
            self.assertEqual(len(db.products()),1)
            self.assertEqual(len(db.history()),2)
            self.assertEqual(len(db.changes()),1)
            self.assertEqual(db.products()[0]["цена сейчас, ₽"],90.0)
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
