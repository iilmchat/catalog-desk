# S24.5 Auchan source mapping review

Source evidence used for this mapping: the user's S24 source sample output dated 2026-11-27 and its per-file manifest.

| Source field | Storage rule | Notes |
|---|---|---|
| `retailer` | Must equal `auchan` | Conflicting labels are rejected |
| `product_id` | `external_product_id = str(raw)` | Preserve raw JSON type/value in observation |
| `name` | `product.name` | Retained as source value |
| `brand` | `product.brand` | Optional |
| `url` | `links.product_url_raw` | Not fetched |
| `image_url` | Asset URL observation | Local heuristic only; unverified |
| `category_path` | Ordered category observations | Full path and order are preserved |
| `category_slug` | Separate category source field | Never substituted for `category_path` |
| `price`, `old_price`, `price_min`, `price_max`, `discount_percent`, and any other supported S24 price keys | One row per present key | Raw state preserved; semantic role stays unclassified |
| `uom` | `commercial.unit_of_measure_raw` | No unit conversion |
| `scraped_at` | Observation timestamp input | Parsed to UTC where valid; raw value preserved |
| all other source fields | `source_record_raw` | No silent field dropping |

## Example from the supplied source sample

- Source ID: numeric `628149`
- Canonical external ID: string `"628149"`
- Category path: `Алкоголь (самовывоз) → Вино → Красное вино`
- Category slug: `alkogol`
- `price`: `1499.97`
- `old_price`: `2799.99`
- `discount_percent`: `46`
- `price_min`: `1499.97`
- `price_max`: `2799.99`

The adapter does not infer that `price_min`/`price_max` represent the current and regular prices. They remain distinct source fields. Currency remains not declared by the source sample.

## Snapshot pin

- File bytes: 67,870,804
- Records: 81,413
- SHA-256: `c938237c01ae2bc881665afa058012aabec9794754cea7059195b5be71dd5a71`
