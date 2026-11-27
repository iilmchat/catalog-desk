# Catalog Desk — S24.5 production import

Это отдельный guarded-runner для переноса шести уже проверенных S24.5 снимков в рабочую SQLite:

- Auchan — `auchan`
- Globus — `globus`
- Dixy — `dixy`
- Chizhik — `chizhik`
- Perekrestok — `perekrestok`
- Pyaterochka — `5ka` (исходный `retailer` остаётся `pyaterochka`)

## Границы безопасности

1. `preflight` повторно проверяет SHA256, структуру, retailer, идентификаторы, ценовые поля, категории и assets всех шести JSON.
2. `preflight` проверяет рабочую БД read-only, требует существующую S24 schema v1 и пустые S24 data tables, создаёт отдельный SQLite Backup API backup и проверяет его целостность.
3. `apply` требует preflight report, проверенный backup и явный флаг `--apply`.
4. Все шесть импортов выполняются одним `BEGIN IMMEDIATE` transaction. Любая ошибка до COMMIT приводит к ROLLBACK всех шести.
5. После импорта проверяются исходные SHA256, no-op повтор каждого импорта, точные количества по каждому ритейлеру и полям, legacy fingerprint и row counts, `quick_check` и FK.
6. Код не вызывает миграции схемы, не пишет в legacy-таблицы, не меняет `schema_meta`/`user_version`, не запускает очереди, enrichment, HTTP-запросы или загрузку фотографий.

Резервная копия и JSON-отчёты остаются на диске; runner их не удаляет и не перезаписывает.

## Пины исходных снимков

| Сеть | Файл | SHA256 | Строк |
|---|---|---|---:|
| Auchan | `auchan_products.json` | `c938237c01ae2bc881665afa058012aabec9794754cea7059195b5be71dd5a71` | 81 413 |
| Globus | `globus_products.json` | `e31965ab77d8a1dfd0da0311f8d2ae2a97f70e9bb7cbec4a86c37ad57b17c266` | 48 158 |
| Dixy | `dixy_products.json` | `8295a61ee1da54ff27638dac2c0d592f1eef59a2caa8362b964af12e1d765777` | 15 311 |
| Chizhik | `chizhik_products.json` | `083bcf673874b073cfeee7c97f0f5e170c8d7dae9a786f6b1424a558c8e0e711` | 3 231 |
| Perekrestok | `perekrestok_products.json` | `677521c71abdbfce439b641c3858195902c7b013b1a1ce1bf644ffb4cfd6c31e` | 6 002 |
| Pyaterochka | `pyaterochka_products.json` | `d78233148c770f00687cf75e10daa32ea19df0157ef58f98c9fc4dc519c6d345` | 5 643 |

Другие файлы, включая `magnit_products.json` и `verno_products.json`, не включены.

## Проверенный итоговый профиль

| Показатель | Ожидается |
|---|---:|
| Исходных observations | 159 758 |
| Подтверждённых retailer products | 152 874 |
| Pending observations Dixy | 6 884 |
| URL identity candidates Dixy | 6 593 |
| Price observations | 767 683 |
| Category observations | 324 760 |
| Asset observations | 159 758 |

Dixy URL candidates записываются только как `unverified`/`collision` metadata; никакой автоматической promotion нет. `price_min`/`price_max` остаются отдельными полями с `semantic_role=unclassified`; валюта не выводится. `category_slug` сохраняется как slug и не подменяет `category_path`/`category_name`.

## Установка и запуск на Windows PowerShell

Распакуйте архив во временную папку, не в корень Git-репозитория. Python: `C:\Python311\python.exe`.

Сначала выполните тесты и preflight. Это ещё не пишет данные в рабочую БД:

```powershell
$repo = "C:\Projects\AI\catalog-desk"
$python = "C:\Python311\python.exe"
$package = Join-Path $env:TEMP "S24_5_production_import_<stamp>\S24_5_production_import"
$sourceDir = "D:\Temp\catalog-desk"
$database = Join-Path $repo "pyaterochka_catalog_v9.sqlite3"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$preflight = Join-Path $env:TEMP "S24_5_production_preflight_$stamp.json"

Push-Location $package
try {
    & $python -m unittest discover -s tests -p "test_*.py" -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed" }

    & $python tools\s24_production_import.py preflight `
        --database $database `
        --source-dir $sourceDir `
        --backup-dir $repo\.. `
        --report-out $preflight
    if ($LASTEXITCODE -ne 0) { throw "Preflight/backup failed" }
}
finally { Pop-Location }
```

После проверки preflight report запускается production import (второй отдельный шаг):

```powershell
$finalReport = Join-Path $env:TEMP "S24_5_production_import_report_<stamp>.json"

Push-Location $package
try {
    & $python tools\s24_production_import.py apply `
        --database $database `
        --source-dir $sourceDir `
        --preflight-report $preflight `
        --report-out $finalReport `
        --apply
    if ($LASTEXITCODE -ne 0) { throw "Production import failed" }
}
finally { Pop-Location }
```

Не запускайте `apply`, если preflight не прошёл полностью, backup отсутствует/невалиден или любой SHA/count отличается.

## Результат

`S24_5_production_import_report.json` содержит per-retailer import results, ожидаемые и фактические счётчики, backup hash, legacy fingerprint, состояние SQLite и подтверждение транзакционного COMMIT. Полный импорт меняет только S24 catalog tables; это не подключает S24 данные к UI/API и не синхронизирует их с legacy X5 enrichment tables.
