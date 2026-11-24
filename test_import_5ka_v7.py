import unittest

import import_5ka_exports_v7 as importer


class PriceParsingTests(unittest.TestCase):
    def test_old_and_sale_price(self):
        current, regular, discount, promo, pct = importer.parse_visible_card(
            "-12% 4,83 Картофель мытый 2.5кг Старая цена 253 99 ₽Цена 226 05 ₽"
        )
        self.assertEqual(current, 226.05)
        self.assertEqual(regular, 253.99)
        self.assertEqual(discount, 226.05)
        self.assertEqual(promo, "-12%")
        self.assertAlmostEqual(pct, 11.0)

    def test_quantity_promo_with_one_price(self):
        current, regular, discount, promo, pct = importer.parse_visible_card(
            "% от 2 шт 4,89 Грибы эринги 200г Цена 189 99 ₽"
        )
        self.assertEqual(current, 189.99)
        self.assertIsNone(regular)
        self.assertIsNone(discount)
        self.assertEqual(promo, "% от 2 шт")
        self.assertIsNone(pct)

    def test_percent_badge_without_old_price(self):
        current, regular, discount, promo, pct = importer.parse_visible_card(
            "-15% Товар Цена 199 99 ₽"
        )
        self.assertEqual(current, 199.99)
        self.assertIsNone(regular)
        self.assertEqual(discount, 199.99)
        self.assertEqual(promo, "-15%")
        self.assertEqual(pct, 15.0)

    def test_thousands_and_kopecks(self):
        current, regular, discount, _, _ = importer.parse_visible_card(
            "Корм Старая цена 4 299 00 ₽Цена 3 999 99 ₽"
        )
        self.assertEqual(regular, 4299.00)
        self.assertEqual(current, 3999.99)
        self.assertEqual(discount, 3999.99)

    def test_api_json_shape_from_user_run(self):
        product = {
            "plu": 4259761,
            "name": "Корм сухой Grandorf",
            "image_links": {
                "normal": ["https://catalog-images.x5static.net/product/1620936-main/800x800.jpeg"]
            },
            "prices": {"regular": "4299.0", "discount": None},
            "is_available": True,
        }
        row = importer.record_from_api_product(product, "Тест", "35XY")
        self.assertEqual(row["PLU"], 4259761)
        self.assertEqual(row["цена сейчас, ₽"], 4299.0)
        self.assertEqual(row["обычная цена, ₽"], 4299.0)
        self.assertTrue(row["фото URL"].endswith("800x800.jpeg"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
