"""Cross-retailer regression checks against saved real product-screen DOM fragments."""
from __future__ import annotations

import unittest
from html.parser import HTMLParser
from pathlib import Path

from product_data_policy_v17 import classify_product, composition_destination


FIXTURES = Path(__file__).parent / "tests" / "fixtures"


class ProductScreenDOM(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_product = False
        self.product_depth = 0
        self.product_roots = 0
        self.product_text = []
        self.title = []
        self.in_title = False
        self.attributes = {}
        self.in_attribute = False
        self.attribute_depth = 0
        self.attribute_parts = []
        self.price = None
        self.images = []
        self.in_breadcrumb = False
        self.breadcrumb_depth = 0
        self.breadcrumb_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        qa = a.get("data-qa", "")
        if qa == "product-screen":
            self.in_product = True
            self.product_depth = 1
            self.product_roots += 1
        elif self.in_product and tag not in self.VOID:
            self.product_depth += 1

        if tag == "nav" and a.get("aria-label") == "breadcrumb":
            self.in_breadcrumb = True
            self.breadcrumb_depth = 1
        elif self.in_breadcrumb and tag not in self.VOID:
            self.breadcrumb_depth += 1

        if self.in_product and qa == "product-card-title":
            self.in_title = True
        if self.in_product and qa == "product-detail-attribute":
            self.in_attribute = True
            self.attribute_depth = 1
            self.attribute_parts = []
        elif self.in_attribute and tag not in self.VOID:
            self.attribute_depth += 1

        if self.in_product and tag == "meta" and a.get("itemprop") == "price":
            self.price = a.get("content")
        if self.in_product and tag == "img" and a.get("itemprop") == "image":
            self.images.append(a.get("src"))

    def handle_endtag(self, tag):
        if self.in_title and tag == "h1":
            self.in_title = False
        if self.in_attribute:
            self.attribute_depth -= 1
            if self.attribute_depth == 0:
                if len(self.attribute_parts) >= 2:
                    self.attributes[self.attribute_parts[0]] = self.attribute_parts[1]
                self.in_attribute = False
        if self.in_breadcrumb:
            self.breadcrumb_depth -= 1
            if self.breadcrumb_depth == 0:
                self.in_breadcrumb = False
        if self.in_product:
            self.product_depth -= 1
            if self.product_depth == 0:
                self.in_product = False

    def handle_data(self, data):
        value = " ".join(data.split())
        if not value:
            return
        if self.in_product:
            self.product_text.append(value)
        if self.in_title:
            self.title.append(value)
        if self.in_attribute:
            self.attribute_parts.append(value)
        if self.in_breadcrumb:
            self.breadcrumb_text.append(value)

    @classmethod
    def from_file(cls, filename):
        parser = cls()
        parser.feed((FIXTURES / filename).read_text(encoding="utf-8"))
        return parser


class CrossRetailerRealDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ProductScreenDOM.from_file("perekrestok_4414429.html")
        cls.agar = ProductScreenDOM.from_file("5ka_4275044.html")

    def test_perekrestok_non_food_real_dom_and_taxonomy(self):
        dom = self.tree
        self.assertEqual(dom.product_roots, 1)
        self.assertEqual(" ".join(dom.title), "Ёлочка искусственная Маркет, 25х25х50см")
        self.assertEqual(dom.price, "1399.99")
        self.assertEqual(len(dom.images), 1)
        self.assertEqual(dom.attributes.get("Вес"), "0.37 кг")
        self.assertEqual(dom.attributes.get("Срок хранения"), "не ограничен")
        self.assertEqual(dom.attributes.get("Бренд"), "Маркет")
        self.assertEqual(dom.attributes.get("Страна производства"), "Китай")
        self.assertIn("Полиэтилен, джут.", " ".join(dom.product_text))
        self.assertIn("Для Нового года", " ".join(dom.breadcrumb_text))
        classification = classify_product(breadcrumbs=" ".join(dom.breadcrumb_text))
        self.assertEqual(classification["product_class"], "non_food")
        self.assertEqual(composition_destination("non_food"), "materials")

    def test_pyaterochka_food_page_real_dom_and_missing_category_evidence(self):
        dom = self.agar
        self.assertEqual(dom.product_roots, 1)
        self.assertEqual(" ".join(dom.title), "Агар Dr. Bakers пищевой 7г")
        self.assertEqual(dom.price, "134.99")
        self.assertEqual(len(dom.images), 1)
        self.assertEqual(dom.attributes.get("Вес"), "0.007 кг")
        self.assertEqual(dom.attributes.get("Срок хранения"), "365 дней")
        self.assertIn("Условия хранения", dom.attributes)
        self.assertEqual(dom.attributes.get("Бренд"), "Dr. Bakers")
        text = " ".join(dom.product_text)
        self.assertIn("0.4", text)
        self.assertIn("174.0", text)
        self.assertIn("оптимальной прочностью геля", text)
        self.assertIn("агар (загуститель, агент желирующий)", text)
        # This captured 5ka page exposes only Главная / Каталог / product.
        # Do not infer a food class from the product title alone.
        classification = classify_product(breadcrumbs=" ".join(dom.breadcrumb_text))
        self.assertEqual(classification["product_class"], "unknown")
        self.assertEqual(composition_destination("unknown"), "unclassified")

    def test_both_real_fixtures_have_expected_semantic_selectors(self):
        for filename in ("perekrestok_4414429.html", "5ka_4275044.html"):
            html = (FIXTURES / filename).read_text(encoding="utf-8")
            self.assertEqual(html.count('data-qa="product-screen"'), 1)
            self.assertEqual(html.count('data-qa="product-card-title"'), 1)
            self.assertGreaterEqual(html.count('data-qa="product-detail-attribute"'), 3)
            self.assertIn('itemprop="price"', html)
            self.assertIn('itemprop="image"', html)


if __name__ == "__main__":
    unittest.main()
