import unittest

from product_data_policy_v17 import (
    applicability_for, calculate_completeness, classify_product,
    composition_destination, is_food_classification,
)


class ProductDataPolicyV17Tests(unittest.TestCase):
    def test_food_category_is_classified_from_taxonomy(self):
        result = classify_product(categories="Фрукты и овощные нарезки")
        self.assertEqual(result["product_class"], "food")
        self.assertEqual(result["classification_basis"], "category_taxonomy")

    def test_non_food_category_is_classified_from_taxonomy_not_title(self):
        result = classify_product(categories="Новогодние товары")
        self.assertEqual(result["product_class"], "non_food")
        self.assertEqual(classify_product(categories="", breadcrumbs="")["product_class"], "unknown")

    def test_pet_food_has_separate_class(self):
        self.assertEqual(classify_product(categories="Корм для кошек")["product_class"], "pet_food")

    def test_non_food_nutrition_is_not_applicable_and_materials_are_required(self):
        applicability = applicability_for("non_food")
        self.assertEqual(applicability["nutrition"], "not_applicable")
        self.assertEqual(applicability["ingredients"], "not_applicable")
        self.assertEqual(applicability["materials"], "applicable")
        values = {
            "name": "Ёлочка искусственная Маркет, 25х25х50см",
            "price": 1399.99, "image": "https://example.invalid/tree.jpg",
            "materials": "Полиэтилен, джут", "weight": "0.37 кг", "brand": "Маркет",
        }
        result = calculate_completeness("non_food", values, applicability)
        self.assertEqual(result["percent"], 100)
        self.assertEqual(result["completed"], 6)
        self.assertEqual(result["required"], 6)
        self.assertEqual(result["fields"]["nutrition"]["status"], "not_applicable")
        self.assertFalse(result["classification_review_required"])

    def test_food_requires_nutrition_and_ingredients(self):
        applicability = applicability_for("food")
        values = {
            "name": "Ананас клубника голубика", "price": 369.99,
            "image": "https://example.invalid/fruit.jpg",
            "nutrition": {"Белки": "1.2", "Жиры": "0.3", "Углеводы": "11.2", "Ккал": "52.7"},
            "weight": "0.15 кг", "brand": "Зелёная Линия", "shelf_life": "5 дней",
        }
        result = calculate_completeness("food", values, applicability)
        self.assertEqual(result["required"], 8)
        self.assertEqual(result["percent"], 88)
        self.assertEqual(result["fields"]["nutrition"]["status"], "captured")
        self.assertEqual(result["fields"]["ingredients"]["status"], "not_found")

    def test_unknown_classification_never_scores_100_percent(self):
        values = {"name": "Товар", "price": 10, "image": "https://example.invalid/a.jpg"}
        result = calculate_completeness("unknown", values, applicability_for("unknown"))
        self.assertEqual(result["percent"], 75)
        self.assertTrue(result["classification_review_required"])
        self.assertEqual(result["fields"]["nutrition"]["applicability"], "unknown")

    def test_ingredient_search_requires_confirmed_food_class(self):
        self.assertTrue(is_food_classification("food"))
        self.assertTrue(is_food_classification("pet_food"))
        self.assertFalse(is_food_classification("non_food"))
        self.assertFalse(is_food_classification("unknown"))
        self.assertTrue(is_food_classification(None, "Фрукты и овощные нарезки"))
        self.assertFalse(is_food_classification(None, "Новогодние товары"))

    def test_generic_composition_label_routes_by_product_class(self):
        self.assertEqual(composition_destination("food"), "ingredients")
        self.assertEqual(composition_destination("non_food"), "materials")
        self.assertEqual(composition_destination("unknown"), "unclassified")


if __name__ == "__main__":
    unittest.main()
