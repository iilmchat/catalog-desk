# Pyaterochka source mapping

Reviewed snapshot: `pyaterochka_products.json`
SHA-256: `d78233148c770f00687cf75e10daa32ea19df0157ef58f98c9fc4dc519c6d345`
Records: 5,643; unique string `product_id`: 5,643.

- Source `retailer` must equal `pyaterochka`; canonical S24 retailer key is `5ka`.
- `product_id` is preserved as a string. Numeric, boolean, empty, or duplicate IDs are rejected.
- `price_min` and `price_max` are retained as separate raw fields with `semantic_role=unclassified`; each appears in all 5,643 records. No semantic meaning or currency is inferred.
- `category_slug` is retained in canonical category metadata and the complete raw record. Since `category_path` and `category_name` are absent, this source contributes zero category observations.
- `url` is retained as product URL; `image_url` is independently classified by the shared S24 asset URL classifier.
- The complete raw source record is retained in `source_record_raw`, including `badge`, `name`, `scraped_at`, and fields not normalized by the current schema.
- The runner performs the import only against a new disposable SQLite copy; the application database is opened read-only.

No semantic interpretation of min/max prices or category slug is inferred.
