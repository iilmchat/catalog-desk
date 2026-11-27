# S24.5 Chizhik source mapping review

## Evidence and pinned snapshot

Source evidence is the user-supplied read-only profile `CHIZHIK_SOURCE_PROFILE_20261127.json`.

| Property | Reviewed value |
|---|---:|
| File | `chizhik_products.json` |
| SHA-256 | `083bcf673874b073cfeee7c97f0f5e170c8d7dae9a786f6b1424a558c8e0e711` |
| Bytes | 2,469,901 |
| Records | 3,231 |
| Unique IDs | 3,231 |
| Duplicate ID groups | 0 |
| ID types | 3,231 integer |
| Missing / invalid IDs | 0 / 0 |
| `price` value / null | 1,131 / 2,100 |
| `promo_price` null | 3,231 |
| `category_path` items | 3,657 |
| Image URL profile | 3,231 URL candidates |

## Field mapping

| Source field | S24 handling | Qualification |
|---|---|---|
| `retailer` | Required exact value `chizhik` | Other labels reject the run |
| `product_id` | `external_product_id = str(raw integer)` | Integer raw value and JSON type retained |
| `name` | Product name | Raw value retained |
| `brand` | Brand | Raw value retained |
| `url` | Product URL | Not fetched |
| `image_url` | Asset observation | Heuristic classification only, unverified |
| `category_path` | Ordered category observations | 3,657 path elements in reviewed profile |
| `category_name` | Additional category observation if present | Profile supplied did not state a separate count; runner derives it from actual raw data |
| `price` | Price observation when key is present | `value` or `null` state retained, semantic role unclassified |
| `promo_price` | Separate price observation when key is present | All 3,231 values are null in the reviewed profile |
| `uom` | Unit of measure raw value | No conversion |
| `scraped_at` | Raw timestamp and parsed UTC timestamp/status | Raw source value retained |
| Other properties | `source_record_raw` | No silent drops or inferred semantics |

The sample evidence included retailer-specific properties such as `store_id`, `stock_limit`, rating information and ordering limits. The schema adapter has no dedicated normalized columns for those source properties; the full raw JSON observation retains them byte-for-value at JSON-value level.

## Price treatment

The source profile shows `price` present in every row: 1,131 non-null values and 2,100 JSON nulls. `promo_price` is present and JSON null in all 3,231 rows. The pilot therefore expects 6,462 price observation rows. Neither the property names nor their values are reinterpreted as a current/regular price, and no currency is assigned.

## Identity treatment

All 3,231 `product_id` values are unique JSON integers. The storage key uses the decimal string form (for example, integer `4424136` becomes external ID text `4424136`), while the original raw integer is retained. String, boolean, floating-point, missing and duplicate IDs cause refusal; this pinned pilot does not generate URL-derived identity candidates.

## Out of scope

No cross-retailer matching, global SKU equivalence, price semantics, currency inference, stock normalization, store-context modeling, image downloading/verification, production import, legacy table update, UI update or enrichment/queue task is performed.
