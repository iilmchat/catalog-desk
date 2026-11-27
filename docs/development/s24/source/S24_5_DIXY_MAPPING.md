# S24.5 Dixy mapping — current snapshot

Snapshot: 2026-11-27; source hash `8295a61ee1da54ff27638dac2c0d592f1eef59a2caa8362b964af12e1d765777`; 15,311 rows.

- 8,427 source-confirmed IDs, all strings, unique.
- 6,884 records have missing `product_id` and remain `pending`.
- 6,593 pending observations have a terminal 10-digit URL ID candidate; all 6,593 candidates are unique.
- No duplicate confirmed IDs, duplicate candidate groups, or candidate collisions with known IDs in the profiled snapshot.
- Candidate source rule: `dixy-terminal-url-id-v1`; only `url` is examined, never `product_key`.

## Identity mapping

| Source condition | Canonical | Storage |
|---|---|---|
| non-empty string/integer `product_id` | `resolved` | retailer-scoped product + observation |
| missing/null/empty `product_id` | `pending` | observation only |
| terminal URL number on approved Dixy product path | candidate metadata | `unverified`, no product promotion |
| candidate matching confirmed ID | collision metadata | `collision`, no merge |
| boolean/float/object ID | invalid source | reject run |

## Field mapping

- Prices present in this snapshot: `price_min` 15,311; `price_max` 15,311; `price_old` 15,278; total expected price observations 45,900.
- Price roles and currency remain unclassified/undeclared.
- Category observations: 15,278, based on source category fields. Keep `category_name`, `category_slug`, and `category_path` separate.
- Asset observations: 15,311; 10,899 URL candidates and 4,412 listing-page URLs by local heuristic; none are network-verified.
- Entire original source record is retained.
