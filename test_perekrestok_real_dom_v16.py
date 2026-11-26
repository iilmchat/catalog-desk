"""Regression checks against the saved, real Perekrestok product DOM."""
from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "perekrestok_4406119.html"
EXPECTED_NAME = "Ананас клубника голубика свежие очищенные нарезанные Зелёная Линия, 150г"


class ProductDOM(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.in_product = False
        self.product_text = []
        self.title_text = []
        self.in_title = False
        self.attrs = {}
        self.in_attr = False
        self.attr_depth = 0
        self.attr_parts = []
        self.images = []
        self.price = None
        self.brand = None
        self.in_brand = False
        self.promo_outside_product = False
        self.in_promo = False
        self.promo_depth = 0
        self.product_roots = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
        if a.get("data-qa") == "product-screen":
            self.in_product = True
            self.depth = 1
            self.product_roots += 1
        elif self.in_product and tag not in void_tags:
            self.depth += 1

        if self.in_product and a.get("data-qa") == "product-card-title":
            self.in_title = True
        if self.in_product and a.get("data-qa") == "product-detail-attribute":
            self.in_attr = True
            self.attr_depth = 1
            self.attr_parts = []
        elif self.in_attr:
            self.attr_depth += 1

        if self.in_product and tag == "img" and a.get("itemprop") == "image":
            self.images.append(a.get("src") or a.get("data-src"))
        if self.in_product and a.get("itemprop") == "price":
            self.price = a.get("content")
        if not self.in_product and a.get("data-qa") == "promo-listing-carousel":
            self.promo_outside_product = True

    def handle_endtag(self, tag):
        if self.in_title and tag == "h1":
            self.in_title = False
        if self.in_attr:
            self.attr_depth -= 1
            if self.attr_depth == 0:
                if len(self.attr_parts) >= 2:
                    self.attrs[self.attr_parts[0]] = self.attr_parts[1]
                self.in_attr = False
        if self.in_product:
            self.depth -= 1
            if self.depth == 0:
                self.in_product = False

    def handle_data(self, data):
        if self.in_product:
            self.product_text.append(data)
        if self.in_title:
            self.title_text.append(data)
        if self.in_attr:
            value = " ".join(data.split())
            if value:
                self.attr_parts.append(value)


class PerekrestokRealDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")
        cls.dom = ProductDOM()
        cls.dom.feed(cls.html)
        cls.text = " ".join(" ".join(cls.dom.product_text).split())

    def test_fixture_is_real_product_card_and_title(self):
        self.assertEqual(self.dom.product_roots, 1)
        self.assertIn(EXPECTED_NAME, " ".join(self.dom.title_text))

    def test_brand_price_images_and_nutrition(self):
        self.assertEqual(self.dom.attrs.get("Бренд"), "Зелёная Линия")
        self.assertEqual(self.dom.price, "369.99")
        self.assertEqual(len([x for x in self.dom.images if x]), 3)
        self.assertEqual(self.dom.attrs.get("Вес"), "0.15 кг")
        self.assertEqual(self.dom.attrs.get("Бренд"), "Зелёная Линия")
        self.assertEqual(self.dom.attrs.get("Страна производства"), "Россия")
        self.assertEqual(self.dom.attrs.get("Белки"), "1.2 г")
        self.assertEqual(self.dom.attrs.get("Жиры"), "0.3 г")
        self.assertEqual(self.dom.attrs.get("Углеводы"), "11.2 г")
        self.assertTrue(re.search(r"52[,.]7", self.text))

    def test_absent_composition_is_not_invented(self):
        self.assertNotRegex(self.text, r"(?i)\bСостав\b")
        self.assertNotIn('data-qa="product-description"', self.html)

    def test_recommendations_are_outside_product_root(self):
        self.assertTrue(self.dom.promo_outside_product)
        self.assertNotIn("Вам понравится", self.text)
        self.assertNotIn("Похожие товары", self.text)
        self.assertNotIn("Отзывы о товаре", self.text)

    def test_dom_contract_selectors_exist(self):
        self.assertIn('data-qa="product-screen"', self.html)
        self.assertIn('data-qa="product-card-title"', self.html)
        self.assertGreaterEqual(self.html.count('data-qa="product-detail-attribute"'), 10)
        self.assertIn('itemprop="price" content="369.99"', self.html)
        self.assertIn('itemprop="image"', self.html)


if __name__ == "__main__":
    unittest.main()
