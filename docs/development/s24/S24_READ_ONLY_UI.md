# S24.5 — read-only multi-retailer catalog UI

## Scope

S24 is a parallel catalog model for the six committed retailer snapshots: Auchan (`auchan`), Globus (`globus`), Dixy (`dixy`), Chizhik (`chizhik`), Perekrestok (`perekrestok`) and Pyaterochka (`5ka`). It is not an extension of the legacy `chain + SAP + PLU` model.

The S24 workspace is a read-only view of the existing S24 schema v1. It does not create tables, migrate data, import JSON, download images, resolve identity candidates, or enqueue enrichment.

## Read-only contract

- `s24/read_model_v1.py` is the UI query layer for S24 tables.
- Connections use SQLite URI `mode=ro` and `PRAGMA query_only=ON`.
- The read model checks that all nine S24 tables exist and that `schema_version=1`; unavailable schema returns `available=false`.
- The HTTP routes are GET-only:
  - `GET /api/s24/summary`
  - `GET /api/s24/products?retailer=all&identity_status=all&q=&page=1&page_size=25`
  - `GET /api/s24/observations/{observation_id}`
  - `GET /api/s24/imports?limit=50`
- No S24 POST or mutation route is registered. The S24 interface does not expose import or identity-resolution actions.

## UI sections

1. **S24 summary:** six retailer rows, confirmed product counts, observations, resolved/pending/invalid identities, price/category/asset observations and candidates.
2. **Import journal:** source filename, abbreviated SHA-256, source/committed record counts, import status and timestamp.
3. **Observation browser:** retailer and identity filters, text search, pagination; rows represent source observations and are not collapsed into product rows.
4. **Observation detail:** raw source JSON, source/import metadata, raw price fields, category observations, asset classifications and identity candidates.

## Data interpretation

- Stable product identity is `(retailer_key, external_product_id)`; identical IDs across retailers are separate identities.
- Pending observations have no stable catalog product and remain visible as pending.
- Dixy URL candidates remain unverified/collision metadata; the UI cannot promote them.
- Price fields remain separate by source field name. `semantic_role` and `currency_status` are displayed as stored; values are not normalized to rubles or given current/regular/promo meanings.
- Category slug/id are metadata; they do not become breadcrumb/category-name observations.
- Image URL classification is heuristic only. Only `verified_image` represents a checked image. URLs are shown as links and never fetched by the UI.
- Legacy `catalog_config.py`, legacy selector, importer and enrichment queues remain for Pyaterochka/Perekrestok only.

## Verified smoke

The local disposable-database HTTP/API smoke report is [S24_5_UI_SMOKE_20261127.md](S24_5_UI_SMOKE_20261127.md). It records 114 local tests, read-only S24 API checks, six import entries, source DB/WAL unchanged, and the remaining manual graphical-browser acceptance.

## Tests

- `tests/test_s24_read_model.py`: schema-unavailable behavior, aggregate counts, filters, related observations, import metadata, raw payload and read-only connection write rejection.
- Full repository test command: `python -m unittest discover -s tests -p "test_*.py" -v`.
- Manual/browser acceptance: [MANUAL_TESTING_GUIDE.md](../../qa/MANUAL_TESTING_GUIDE.md), section 8.

## Current source boundaries

S24.5 includes only the six pinned source snapshots recorded in [S24_5_PRODUCTION_IMPORT.md](S24_5_PRODUCTION_IMPORT.md). Magnit, Verny and Tvoydom are not part of this schema/import snapshot and must not be shown as imported in the S24 screen. The later Pyaterochka file with 7,134 records is a different snapshot from the committed 5,643-record import baseline. `dixy_details.json` is a separate detail stream, not a seventh catalog.
