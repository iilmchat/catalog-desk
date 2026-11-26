"""Category-aware product data quality policy (V17).

Applicability is separate from extraction outcome. Unknown classification is never
silently treated as not-applicable. The module is deliberately pure and SQLite-free.
"""
from __future__ import annotations

import re
from typing import Any

POLICY_VERSION = "1"
PRODUCT_CLASSES = {"food", "pet_food", "non_food", "unknown"}

FOOD_CATEGORY_TERMS = (
    "кафе", "продукты", "продукт", "фрукты", "овощи", "мяс", "рыб",
    "молоч", "сыр", "колбас", "хлеб", "выпеч", "бакале", "напит",
    "сок", "сладост", "кондитер", "заморожен", "готовая еда",
    "гастроном", "яйц", "орех", "круп", "макарон", "консервы",
)
PET_FOOD_TERMS = ("корм для животных", "корма для животных", "корм для кошек",
                  "корм для собак", "зоокорм", "лакомства для животных")
NON_FOOD_TERMS = (
    "бытовая химия", "товары для дома", "дом и интерьер", "новогодние товары",
    "сезонные товары", "игрушки", "посуда", "текстиль", "электроника",
    "сад и огород", "садовые товары", "декор", "искусственные елки",
    "искусственные ёлки", "канцтовары", "товары для животных", "косметика",
    "гигиена", "уход за домом", "хозтовары",
)

UNIVERSAL_REQUIRED = ("name", "price", "image")
REQUIRED_BY_CLASS = {
    "food": UNIVERSAL_REQUIRED + ("ingredients", "nutrition", "weight", "shelf_life", "brand"),
    "pet_food": UNIVERSAL_REQUIRED + ("ingredients", "nutrition", "weight", "brand"),
    "non_food": UNIVERSAL_REQUIRED + ("materials", "weight", "brand"),
    # Classification itself remains a required gate; no unknown product can score 100%.
    "unknown": UNIVERSAL_REQUIRED + ("classification",),
}

FIELD_LABELS = {
    "name": "Название", "price": "Цена", "image": "Фото",
    "ingredients": "Пищевые ингредиенты", "nutrition": "БЖУ",
    "materials": "Материалы", "weight": "Вес/объём",
    "shelf_life": "Срок хранения", "brand": "Бренд",
    "classification": "Классификация",
}


def _norm(value: Any) -> str:
    return re.sub(r"\\s+", " ", str(value or "").casefold()).strip()


def classify_product(categories: Any = "", breadcrumbs: Any = "") -> dict:
    """Classify from explicit taxonomy/breadcrumbs only; never infer from title alone."""
    taxonomy = _norm(" ".join(categories) if isinstance(categories, (list, tuple, set)) else categories)
    crumbs = _norm(" ".join(breadcrumbs) if isinstance(breadcrumbs, (list, tuple, set)) else breadcrumbs)
    evidence = " | ".join(x for x in (taxonomy, crumbs) if x)
    if not evidence:
        return {"product_class": "unknown", "classification_basis": "no_taxonomy", "classification_confidence": 0.0}
    if any(term in evidence for term in PET_FOOD_TERMS):
        return {"product_class": "pet_food", "classification_basis": "category_taxonomy", "classification_confidence": 1.0}
    if any(term in evidence for term in NON_FOOD_TERMS):
        return {"product_class": "non_food", "classification_basis": "category_taxonomy", "classification_confidence": 1.0}
    if any(term in evidence for term in FOOD_CATEGORY_TERMS):
        return {"product_class": "food", "classification_basis": "category_taxonomy", "classification_confidence": 1.0}
    return {"product_class": "unknown", "classification_basis": "unmapped_taxonomy", "classification_confidence": 0.0}


def applicability_for(product_class: str) -> dict:
    """Return applicability for domain-specific fields, not extraction status."""
    cls = product_class if product_class in PRODUCT_CLASSES else "unknown"
    result = {
        "nutrition": "unknown", "ingredients": "unknown", "materials": "unknown",
        "shelf_life": "unknown", "weight": "applicable", "brand": "applicable",
    }
    if cls in {"food", "pet_food"}:
        result.update(nutrition="applicable", ingredients="applicable",
                      materials="not_applicable", shelf_life="applicable" if cls == "food" else "unknown")
    elif cls == "non_food":
        result.update(nutrition="not_applicable", ingredients="not_applicable",
                      materials="applicable", shelf_life="not_applicable")
    return result


def composition_destination(product_class: str, source_label: str = "Состав") -> str:
    """Map a retailer's generic 'Состав' label to a semantic destination."""
    return "materials" if product_class == "non_food" else ("ingredients" if product_class in {"food", "pet_food"} else "unclassified")


def calculate_completeness(product_class: str, values: dict, applicability: dict | None = None) -> dict:
    """Score required applicable fields; unknown applicability is not N/A."""
    cls = product_class if product_class in PRODUCT_CLASSES else "unknown"
    applicability = applicability or applicability_for(cls)
    required = REQUIRED_BY_CLASS[cls]
    present = {
        "name": bool(values.get("name")),
        "price": values.get("price") is not None and str(values.get("price")).strip() != "",
        "image": bool(values.get("image")),
        "ingredients": bool(values.get("ingredients")),
        "nutrition": bool(values.get("nutrition")),
        "materials": bool(values.get("materials")),
        "weight": bool(values.get("weight")),
        "shelf_life": bool(values.get("shelf_life")),
        "brand": bool(values.get("brand")),
        "classification": cls != "unknown",
    }
    # A required domain field with unknown applicability remains unresolved and
    # cannot be counted as complete. Known N/A fields are excluded by class policy.
    applicable_required = [key for key in required if key in UNIVERSAL_REQUIRED or key == "classification"
                           or applicability.get(key) == "applicable"]
    denominator = len(applicable_required)
    completed = [key for key in applicable_required if present.get(key, False)]
    fields = {}
    for key in set(REQUIRED_BY_CLASS["food"] + REQUIRED_BY_CLASS["non_food"] + ("classification",)):
        if key in applicability:
            state = applicability[key]
        elif key in UNIVERSAL_REQUIRED or key == "classification":
            state = "applicable"
        else:
            state = "unknown"
        fields[key] = {
            "label": FIELD_LABELS[key],
            "applicability": state,
            "status": "captured" if present.get(key, False) else ("not_applicable" if state == "not_applicable" else "not_found"),
            "required": key in applicable_required,
        }
    return {
        "percent": round(len(completed) / denominator * 100) if denominator else 0,
        "completed": len(completed), "required": denominator,
        "product_class": cls, "fields": fields,
        "classification_review_required": cls == "unknown",
        "policy_version": POLICY_VERSION,
    }
