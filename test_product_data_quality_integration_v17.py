import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import catalog_ui_server_v14 as server
from product_data_policy_v17 import applicability_for


class ProductDataQualityIntegrationV17Tests(unittest.TestCase):
    def test_non_food_generic_composition_is_saved_as_materials_not_ingredients(self):
        product = {
            "name": "Ёлочка искусственная Маркет, 25х25х50см",
            "product_class": "non_food",
            "category_path": "Каталог / Новогодние товары",
            "composition": "Полиэтилен, джут.",
            "product_url": "https://www.perekrestok.ru/product/example--4414429",
        }
        composition, materials, attrs, meta = server._prepare_product_semantics(
            product, {}, {"Вес": "0.37 кг", "Бренд": "Маркет"}, "Состав Полиэтилен, джут. Вес 0.37 кг"
        )
        self.assertIsNone(composition)
        self.assertEqual(materials, "Полиэтилен, джут.")
        self.assertEqual(meta["composition_kind"], "materials")
        self.assertEqual(attrs["_catalog_meta"]["field_applicability"]["nutrition"], "not_applicable")

    def test_unknown_composition_is_preserved_but_not_indexed_as_ingredients(self):
        composition, materials, attrs, meta = server._prepare_product_semantics(
            {"name": "Неизвестный товар", "composition": "Полимер, металл"},
            {}, {}, "Состав Полимер, металл"
        )
        self.assertIsNone(composition)
        self.assertIsNone(materials)
        self.assertEqual(meta["unclassified_composition_text"], "Полимер, металл")
        quality = server.quality_fields({
            "name": "Неизвестный товар", "composition": "Полимер, металл",
            "current_price": 10, "image_url": "https://example.invalid/item.jpg",
            "attributes": attrs, "nutrition": {},
        })
        self.assertFalse(quality["fields"]["состав"])
        self.assertEqual(quality["field_status"]["ingredients"]["applicability"], "unknown")

    def test_non_food_complete_card_can_reach_100_without_nutrition(self):
        product = {
            "name": "Ёлочка искусственная Маркет, 25х25х50см",
            "current_price": 1399.99,
            "image_url": "https://example.invalid/tree.jpg",
            "composition": None,
            "nutrition_json": "{}",
            "attributes": {
                "Вес": "0.37 кг", "Бренд": "Маркет",
                "_catalog_meta": {
                    "product_class": "non_food",
                    "field_applicability": applicability_for("non_food"),
                    "materials_text": "Полиэтилен, джут.",
                },
            },
        }
        quality = server.quality_fields(product)
        self.assertEqual(quality["percent"], 100)
        self.assertEqual(quality["product_class"], "non_food")
        self.assertEqual(quality["field_status"]["nutrition"]["status"], "not_applicable")

    def test_legacy_non_food_composition_is_excluded_from_ingredient_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = server.DB_PATH
            server.DB_PATH = Path(tmp) / "catalog.sqlite3"
            try:
                conn = sqlite3.connect(server.DB_PATH)
                conn.executescript("""
                    CREATE TABLE products(
                        chain TEXT, sap_code TEXT, plu TEXT, name TEXT,
                        current_price REAL, product_url TEXT, categories TEXT
                    );
                    CREATE TABLE product_details(
                        chain TEXT, sap_code TEXT, plu TEXT, composition TEXT, attributes_json TEXT
                    );
                    INSERT INTO products VALUES(
                        'perekrestok','XSEO','4414429','Ёлочка искусственная',1399.99,
                        'https://www.perekrestok.ru/product/example--4414429','Новогодние товары'
                    );
                """)
                conn.execute(
                    "INSERT INTO product_details VALUES(?,?,?,?,?)",
                    ("perekrestok","XSEO","4414429","Полиэтилен, джут",
                     json.dumps({"_catalog_meta":{"product_class":"non_food"}}, ensure_ascii=False))
                )
                conn.commit()
                conn.close()
                result = server.ingredients_data({"chain":["perekrestok"],"ingredient":["джут"]})
                self.assertEqual(result["total"], 0)
            finally:
                server.DB_PATH = old_db

    def test_unknown_class_does_not_treat_missing_nutrition_as_not_applicable(self):
        quality = server.quality_fields({
            "name": "Неизвестный товар", "current_price": 100,
            "image_url": "https://example.invalid/item.jpg",
            "attributes": {}, "nutrition": {},
        })
        self.assertLess(quality["percent"], 100)
        self.assertTrue(quality["classification_review_required"])
        self.assertEqual(quality["field_status"]["nutrition"]["applicability"], "unknown")


if __name__ == "__main__":
    unittest.main()
