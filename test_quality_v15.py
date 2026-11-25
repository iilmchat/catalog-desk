"""Тесты S20: восстановление качества из текста и индикатор полноты."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("server_quality", ROOT / "catalog_ui_server_v14.py")
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(mod)

TEXT = (
    "Главная Каталог Сыр Плавленый сыр Сыр плавленый Hochland Сэндвич ломтики 45%, 150г "
    "4,90 (92 оценки) Hochland 150 г Цена 159 99 ₽ 159,99 ₽/шт В корзину "
    "Пищевая ценность на 100 г 284.0 Ккал 14.5 Белки 23.0 Жиры 4.1 Углеводы "
    "Описание Сыр плавленый Hochland «Сэндвич» это нежный сливочный сыр в формате "
    "ломтиков. Показать полностью Состав Сыры, масло сливочное, молоко сухое "
    "обезжиренное, соль, вода питьевая. Вес 0.15 кг Срок хранения 180 дн. "
    "Условия хранения при температуре от 0°С до +4°С Бренд Hochland"
)


class QualityRepairTests(unittest.TestCase):
    def test_composition_and_description_recovery(self):
        self.assertEqual(
            mod.recover_composition_from_text(TEXT),
            "Сыры, масло сливочное, молоко сухое обезжиренное, соль, вода питьевая",
        )
        self.assertIn("нежный сливочный сыр", mod.recover_description_from_text(TEXT))

    def test_nutrition_repair_from_shifted_values(self):
        self.assertEqual(
            mod.parse_nutrition_from_details_text(TEXT),
            {"Белки": "14.5", "Жиры": "23.0", "Углеводы": "4.1", "Ккал": "284.0"},
        )

    def test_quality_indicator_and_contradiction(self):
        quality = mod.quality_fields({
            "name": "Сыр 250 г", "current_price": 159.99,
            "image_url": "https://catalog-images.x5static.net/product/1-main/320x320.jpeg",
            "composition": "Сыры, соль", "nutrition_json": '{"Белки":"14.5"}',
            "attributes_json": '{"Вес":"0.3 кг","Срок хранения":"180 дн.","Бренд":"Hochland"}',
            "details_text": TEXT,
        })
        self.assertGreaterEqual(quality["percent"], 87)
        self.assertTrue(quality["fields"]["состав"])
        self.assertFalse(quality["fields"]["БЖУ"])
        self.assertIn("не лабораторная", quality["disclaimer"])
        self.assertIsNotNone(quality["contradictions"])
        self.assertEqual(quality["contradictions"]["attribute_value"], 300)

    def test_nutrition_requires_all_four_values(self):
        complete = {"Белки": "14.5", "Жиры": "0", "Углеводы": "4.1", "Ккал": "284"}
        self.assertTrue(mod.has_complete_nutrition(complete))
        self.assertTrue(mod.has_complete_nutrition(json.dumps(complete, ensure_ascii=False)))

        for partial in (
            {},
            {"Белки": "14.5"},
            {"Белки": "14.5", "Жиры": "0", "Углеводы": "4.1"},
            {"Белки": "14.5", "Жиры": "", "Углеводы": "4.1", "Ккал": "284"},
        ):
            self.assertFalse(mod.has_complete_nutrition(partial))

        score, blocks = mod._detail_coverage({
            "composition": "Состав",
            "description": "Описание",
            "nutrition_json": json.dumps({"Белки": "14.5"}),
            "attributes_json": "{}",
            "details_text": "Текст карточки",
        })
        self.assertEqual(score, 60)
        self.assertNotIn("Пищевая ценность", blocks)

        score, blocks = mod._detail_coverage({
            "composition": "Состав",
            "description": "Описание",
            "nutrition_json": json.dumps(complete, ensure_ascii=False),
            "attributes_json": "{}",
            "details_text": "Текст карточки",
        })
        self.assertEqual(score, 80)
        self.assertIn("Пищевая ценность", blocks)

    def test_import_repairs_card_24139(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        old = {k: getattr(mod, k) for k in ("BASE_DIR", "DB_PATH", "DETAIL_DIR")}
        mod.BASE_DIR = root
        mod.DB_PATH = root / "test.sqlite3"
        mod.DETAIL_DIR = root / "pr_product_details"
        mod.DETAIL_DIR.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: [setattr(mod, k, v) for k, v in old.items()])

        conn = sqlite3.connect(mod.DB_PATH)
        conn.executescript("""
        CREATE TABLE exports(file_name TEXT, category TEXT, sap_code TEXT, page_number INTEGER,
            product_rows INTEGER, unique_products INTEGER, captured_at TEXT, imported_at TEXT, source_url TEXT);
        CREATE TABLE products(chain TEXT NOT NULL DEFAULT '5ka', sap_code TEXT NOT NULL, plu TEXT NOT NULL,
            name TEXT, categories TEXT, product_url TEXT, current_price REAL, regular_price REAL,
            promo_price REAL, discount_pct REAL, promo_label TEXT, image_url TEXT, image_file TEXT,
            first_seen_at TEXT, last_seen_at TEXT, first_imported_at TEXT, last_imported_at TEXT,
            PRIMARY KEY(chain,sap_code,plu));
        CREATE TABLE product_categories(chain TEXT, sap_code TEXT, plu TEXT, category TEXT);
        CREATE TABLE price_history(id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, source_hash TEXT,
            sap_code TEXT, plu TEXT, current_price REAL, captured_at TEXT, imported_at TEXT);
        INSERT INTO products(chain,sap_code,plu,name,product_url,current_price,image_url,first_seen_at,last_seen_at,first_imported_at,last_imported_at)
            VALUES('perekrestok','XSEO','24139','Сыр плавленый Hochland Сэндвич 45%, 150г',
            'https://www.perekrestok.ru/product/syr--24139',159.99,
            'https://catalog-images.x5static.net/product/1-main/800x800.jpeg',
            '2026-11-24','2026-11-24','2026-11-24','2026-11-24');
        """)
        conn.commit()
        conn.close()
        mod.ensure_ui_columns()

        payload = {
            "schema_version": 2, "chain": "perekrestok",
            "source": "perekrestok.ru-visible-product-details-export",
            "captured_at": "2026-11-24T20:07:56.747Z",
            "source_url": "https://www.perekrestok.ru/product/syr--24139",
            "store": {"sap_code": "XSEO"},
            "product": {
                "plu": "24139", "name": "Сыр плавленый Hochland Сэндвич 45%, 150г",
                "image_url": "https://catalog-images.x5static.net/product/1-main/800x800.jpeg",
                "composition": None, "description": None,
                "nutrition": {"Белки": "23.0", "Жиры": "4.1", "Ккал": "14.5"},
                "attributes": {"Вес": "0.15 кг", "Срок хранения": "180 дн.", "Бренд": "Hochland"},
                "details_text": TEXT,
            },
        }
        path = mod.DETAIL_DIR / "pr_detail_XSEO_24139.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        files, products_count, skipped, errors = mod.import_pending_details()
        self.assertEqual(errors, [])
        self.assertEqual((files, products_count), (1, 1))

        conn = sqlite3.connect(mod.DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT composition,description,nutrition_json FROM product_details "
            "WHERE chain='perekrestok' AND plu='24139'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["composition"], mod.recover_composition_from_text(TEXT))
        self.assertIn("нежный сливочный сыр", row["description"])
        nutrition = json.loads(row["nutrition_json"])
        self.assertEqual(nutrition, {"Белки": "14.5", "Жиры": "23.0", "Углеводы": "4.1", "Ккал": "284.0"})
        conn.close()


if __name__ == "__main__":
    unittest.main()
