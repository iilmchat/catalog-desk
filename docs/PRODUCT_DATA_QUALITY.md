# Product data quality policy (V17 foundation)

## Purpose

Catalog Desk supports food, pet food, household chemicals, home goods and seasonal
assortment. A field being empty is not enough to decide whether a product card is
incomplete: first determine whether the field applies to that product class.

This policy is additive and backward-compatible. It does not rewrite existing
SQLite rows. Metadata for new captures is stored under the reserved
`attributes_json._catalog_meta` object; legacy `composition` remains the food
ingredient field.

## Product classes

| Class | Meaning |
|---|---|
| `food` | Food and beverages for people |
| `pet_food` | Food and treats for pets |
| `non_food` | Household, home, seasonal and other non-food goods |
| `unknown` | Category evidence is absent or not mapped |

Classification uses explicit category taxonomy / page breadcrumbs. Product title
alone is not used to infer a class. An unmapped category remains `unknown` and
requires review; it must not silently become non-food.

The current keyword map is a conservative first version. Extend it using observed
retailer category names and regression tests, not title-only heuristics.

## Applicability and extraction outcome

Applicability and extraction outcome are separate dimensions.

- Applicability: `applicable`, `not_applicable`, `unknown`.
- Extraction outcome: `captured`, `not_found`, `not_visible`, `parse_error`. The current
DOM collectors emit `captured` / `not_found` (and `not_applicable` where appropriate);
`not_visible` and `parse_error` are reserved for source-specific collectors that can
reliably distinguish those cases.

Known `not_applicable` fields are excluded from the completeness denominator.
Unknown applicability is not treated as N/A. A product with unknown class cannot
reach 100% because classification itself remains unresolved.

Initial policy:

| Field | Food | Pet food | Non-food |
|---|---|---|---|
| Nutrition / BJU | Applicable | Applicable | Not applicable |
| Food ingredients | Applicable | Applicable | Not applicable |
| Materials | Not applicable | Not applicable | Applicable |
| Weight / volume | Applicable | Applicable | Applicable |
| Shelf life | Applicable | Review by category | Not applicable |
| Brand | Applicable | Applicable | Applicable |

The table is a policy baseline, not a claim that every retailer exposes every
attribute. In particular, materials are applicable to non-food but optional in the
broad non-food class; category-specific profiles (e.g. seasonal decor, home goods,
household chemicals) should define stricter required attributes when real retailer
taxonomy and page fixtures are available.

## Composition semantics

Retailer pages may use the generic label "Состав" for different concepts.

- Food and pet food: store it as food ingredients in legacy `composition`.
- Non-food: store it as `materials_text` in catalog metadata; do not put it in
  the ingredient index.
- Unknown class: retain the raw value as `unclassified_composition_text` for
  review; do not treat it as food ingredients.

The original visible page text remains in `details_text`. The metadata records
classification basis, confidence, field applicability, extraction outcome and
source URL. Ingredient and sugar analysis is restricted to confirmed food / pet-food
classes; non-food and unresolved legacy compositions are excluded rather than
interpreted as ingredients.

## Completeness

Completeness is the percentage of required, applicable fields successfully
captured for the product class. The API exposes the exact percentage and
completed/required counts. The enrichment queue currently retains its existing
20-point display buckets for compatibility.

The first policy requires:

- Food: name, price, image, ingredients, complete BJU, weight/volume, shelf life,
  and brand.
- Pet food: name, price, image, ingredients, complete BJU, weight/volume, and brand.
- Non-food baseline: name, price, image, weight/volume, and brand. Materials are captured when present and shown separately, but are not universally mandatory for every non-food category.
- Unknown: name, price, image, and resolved classification.

A non-food product can therefore be 100% complete without BJU. A food product
with no BJU remains incomplete; an unknown product is never assumed to have
inapplicable BJU.

Completeness is separate from accuracy, freshness, provenance and confidence.
A 100% score means required applicable fields were captured, not that their
values have been independently verified.

## Compatibility and rollout

- No SQLite migration or mass data rewrite is part of this change.
- New metadata is embedded in the existing attributes JSON object under
  `_catalog_meta`; normal user-facing attributes must ignore this reserved object.
- Existing records without metadata are classified from stored category labels
  where possible; otherwise they remain unknown and are not granted a false 100%.
- Future schema normalization into dedicated columns can be considered after
  the model has been exercised on real product pages from both retailer DOMs.
- Never launch bulk enrichment or alter existing database rows as part of tests.
