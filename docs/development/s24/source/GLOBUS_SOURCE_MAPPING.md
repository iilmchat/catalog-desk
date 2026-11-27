# Catalog Desk S24.5 — Globus pilot (disposable DB only)

This package reuses the exact S24.3.1 `schema_v1.py` and `storage_v1.py` modules from the uploaded archive (SHA-256 `a1c2fa4f2387a50d8bf50f949ce05dcdd45a7bad9ba35f7148792c2149ae019d`). It provides a Globus-specific canonical mapper and a guarded pilot runner. The CLI pins the approved source SHA-256 and row count; it does not expose command-line overrides for those baselines.

## Safety contract

- Opens the application DB using SQLite URI `mode=ro` and `PRAGMA query_only=ON`.
- Copies it with SQLite Backup API to a **new, non-existing disposable DB** under the explicitly supplied output directory.
- Refuses to reuse an existing output directory or DB.
- Verifies S24 schema version 1 is installed and all S24 data tables are empty before pilot load.
- Validates the Globus JSON SHA-256, expected row count, retailer label, product ID presence and uniqueness before copying/loading.
- Writes only to the disposable DB. It never accepts a production write target.
- Reuses `ingest_one_source_bulk` from S24.3.1.
- Repeats the same retailer+SHA import through the storage idempotency path and requires a no-op.
- Verifies all legacy table/index/trigger/view DDL and row counts remain unchanged in the disposable copy.
- Verifies source DB main-file and WAL fingerprints and source JSON SHA-256 before/after.
- Does not contact retailer sites, download images, run queue work, or alter source JSON.

## Expected source snapshot

- file: `globus_products.json`
- SHA-256: `e31965ab77d8a1dfd0da0311f8d2ae2a97f70e9bb7cbec4a86c37ad57b17c266`
- records: `48158`

The runner fails closed if the file differs. If the collection is updated, do not override the hash casually; perform a new source audit and update the approved baseline deliberately.

## Run tests (Python 3.11+)

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: `Ran 15 tests ... OK`.

## Run the pilot

```powershell
python tools/s24_globus_pilot.py `
  --source-db "C:\Projects\AI\catalog-desk\pyaterochka_catalog_v9.sqlite3" `
  --source-json "D:\Temp\catalog-desk\globus_products.json" `
  --out-dir "$env:TEMP\catalog-desk_s24_5_globus_pilot_YYYYMMDD_HHMMSS"
```

The output directory must not exist. It will contain:

- `globus_pilot.sqlite3` — disposable copy of the current migrated application DB, with only Globus S24 pilot data added;
- `globus_pilot_report.json` — detailed validation report.

Do not point `--out-dir` into the repository DB folder and do not set it to the working database path.

## Acceptance conditions

For the approved snapshot, expected checks include:

- 48,158 source records;
- 48,158 unique confirmed Globus product IDs;
- 48,158 observations;
- 0 pending identities and 0 identity candidates;
- all actual source price fields saved individually, without semantic role or currency guessing;
- each actual source category path item saved with its original position;
- 48,158 asset observations, still `unverified`;
- second same-hash import returns `skipped_existing_import=true`, inserts zero rows;
- all legacy V10 objects and row counts unchanged;
- SQLite `quick_check=ok`, foreign key violations=0;
- production DB and source JSON fingerprints unchanged.

The number of price/category observations is calculated from the actual source file during preflight and checked against the stored rows; it is not hardcoded.
