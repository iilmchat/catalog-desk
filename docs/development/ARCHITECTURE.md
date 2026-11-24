# Архитектура Catalog Desk

**Версия документа:** 0.1 · **Статус:** V14 работает, V15 — проект
**Связанные:** [RULES.md](RULES.md) · [ADR](adr/README.md) · [дизайн V15](v15/DESIGN.md)

---

## 1. Текущая архитектура (V14)

```
браузер пользователя (5ka.ru)
 ├─ bookmarklet «категории»        → 5ka_category_lists/*.json   (чек-лист)
 ├─ bookmarklet «текущая страница» → 5ka_exports/*.json          (товары + цены в raw_text)
 ├─ bookmarklet «карточка товара»  → 5ka_product_details/*.json  (состав, БЖУ, галерея)
 └─ userscript Tampermonkey        → POST /api/batch/record      → batch_detail_*.json
                                             │
                     catalog_ui_server_v14.py │  HTTP :8767, stdlib
                     ├─ ensure_ui_columns()   │  идемпотентные миграции
                     ├─ import_pending_details()
                     ├─ batch_* / enrichment_*
                     └─ /api/* + ui_v14/
                                             ▼
                         pyaterochka_catalog_v9.sqlite3
                                             ▼
                         import_5ka_catalog_v9.py → Excel / CSV
```

**Принцип:** сборщики читают только DOM уже открытой страницы. Никаких запросов
к API, cookies, обхода CAPTCHA, параллельных запросов (ADR-001, ADR-005).

---

## 2. Слои и ответственность

| Слой | Файлы | Ответственность |
| :--- | :--- | :--- |
| Сбор | `5ka_export_*.js`, bookmarklet'ы, `5ka_batch_enrichment_v14.user.js` | извлечь видимые поля, сохранить JSON с `source`, `captured_at`, `store` |
| Приём | `catalog_ui_server_v14.py` (`upload_payload`, `batch_record`) | проверить `source`/SAP/PLU, записать файл, вызвать импорт |
| Импорт | `import_pending_details()`, `import_5ka_catalog_v9.py` | дедупликация по SHA-256, upsert, история цен/снимков, фото |
| Хранилище | SQLite | 15 таблиц, ключ `(sap_code, plu)` |
| Представление | `ui_v14/` + `/api/*` | каталог, аналитика, очередь, пакет, экспорт |

---

## 3. Схема данных V9 (основные таблицы)

| Таблица | Ключ | Роль |
| :--- | :--- | :--- |
| `products` | `(sap_code, plu)` | текущее состояние товара |
| `price_history` | `id`, `UNIQUE(source_hash, sap_code, plu)` | история цен, `captured_at`/`imported_at` |
| `product_categories` | `(sap_code, plu, category)` | история категорий |
| `exports` | `source_hash` | журнал выгрузок, страница, URL, SAP |
| `product_details` | `(sap_code, plu)` | текущий состав/БЖУ/атрибуты/текст |
| `product_detail_history` | `id`, `UNIQUE(source_hash, sap_code, plu)` | снимки карточки |
| `product_images` | `id`, `UNIQUE(sap_code, plu, image_url)` | галерея, `source = catalog/details` |
| `enrichment_tasks` / `_events` | `(sap_code, plu)` | очередь обогащения и журнал |
| `enrichment_batch_state` | `id = 1` | состояние текущего пакета |
| `favorites` | `(sap_code, plu)` | избранное |

Индексы: `idx_history_product_time`, `idx_exports_category_page`,
`idx_product_images_product`, `idx_enrichment_tasks_status`, и др.

**Проблема:** нет `chain`. SAP — контекст доставки, а не сеть; при совпадающих PLU
и запросе без SAP интерфейс смешивает товары двух магазинов (CD-001, CD-009).

---

## 4. Целевая архитектура V15

```
STORES (конфиг) ──► 5ka-адаптер      ─┐
                   perekrestok-адаптер ├─► канонический Export v2 (chain, sap, plu)
                   manual/CSV         ─┘            │
                                                    ▼
                                   импортёр (chain-aware, idempotent)
                                                    ▼
                                   SQLite v10: (chain, sap_code, plu)
                                                    ▼
                          UI: переключатель сети + сравнение двух сетей
                                                    ▼
                          локальный API v1 (обязательный chain)
```

### 4.1. Конфиг сети (единый источник)

`chain`, `sap_codes`, `domain`, `product_url_prefix`, `category_url_regex`,
`image_hosts`, `referer`, `source_markers`, `filename_prefix`, `brand`, `paths`.

### 4.2. Ключи и происхождение

- `(chain, sap_code, plu)` — естественный ключ товара;
- `source_hash` + `captured_at` + `imported_at` — идемпотентность и lineage;
- `field_provenance` (каталог / карточка / история цен) — для индикатора полноты.

### 4.3. Межсетевое сравнение

PLU в X5 общий (110 совпадений в тестовой выгрузке), но цены зависят от сети,
даты и адреса доставки. Сравнение всегда включает `chain` и дату; для товаров
с разной упаковкой используется цена за кг/литр.

---

## 5. Потоки данных

1. **Каталог:** DOM → `raw_text` → regex цен (`PRICE_RE`) → `products`/`price_history`.
2. **Карточка:** DOM → `product` JSON → `product_details` + снимок + `product_images`.
3. **Очередь:** incomplete-карточки → `enrichment_tasks` → batch → шаг 2.
4. **Качество:** `details_text` → fallback composition/description/БЖУ → indicators.
5. **Экспорт:** SQLite → Excel/CSV (с колонкой «Сеть» в V15).

---

## 6. Ограничения

- Сервер доступен только с `127.0.0.1`; авторизации нет.
- Пакетный режим — последовательный, один товар за раз, задержки 12–60 сек.
- Нет фонового ежедневного сбора (V15+), нет внешнего API (V15+).
- Состав/БЖУ собираются только если видны в карточке (CD-003, CD-004).
- `pandas/openpyxl` нужны только для Excel-экспорта.
