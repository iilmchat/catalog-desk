# Perekrestok source mapping

Reviewed source snapshot: `perekrestok_products.json`
SHA-256: `677521c71abdbfce439b641c3858195902c7b013b1a1ce1bf644ffb4cfd6c31e`
Records: 6,002; unique string product IDs: 6,002.

- `retailer` must equal `perekrestok`.
- `product_id` is preserved as a string; numeric IDs are rejected.
- `price_min` and `price_max` are stored as separate raw source fields with `semantic_role=unclassified`.
- Each price field exists in 4,011 records. Missing fields are not materialized; no missing price values are invented.
- `category_slug` is retained in the canonical category metadata and raw source record. This snapshot has no `category_path` or `category_name`, so expected category observation count is zero.
- `image_url` is classified by the shared S24 classifier; raw URL is retained.
- Complete source record is retained as `source_record_raw`, including fields not normalized by the current schema.
- This pilot only writes to a newly created disposable SQLite copy. The source application DB is opened read-only.

No semantic interpretation of min/max prices or category slugs is inferred.
