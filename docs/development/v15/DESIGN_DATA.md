# Дизайн V15 — данные, схема и качество

**Связанные:** [DESIGN.md](DESIGN.md) · [CD-ADR-002](../adr/CD-ADR-002-chain-in-data-model.md) · [CD-ADR-006](../adr/CD-ADR-006-server-side-quality-repair.md)

---

## 1. Схема v10 (изменения против v9)

Добавить `chain TEXT NOT NULL DEFAULT '5ka'` в таблицы:

`products`, `product_categories`, `exports`, `price_history`, `product_details`,
`product_detail_history`, `product_detail_imports`, `product_images`,
`enrichment_tasks`, `enrichment_task_events`, `favorites`, `enrichment_batch_state`.

Ключи/индексы:

| Таблица | Было | Стало |
| :--- | :--- | :--- |
| `products` | `PK(sap_code, plu)` | `PK(chain, sap_code, plu)` |
| `price_history` | `UNIQUE(source_hash, sap_code, plu)` | `UNIQUE(source_hash, chain, sap_code, plu)` |
| `product_details` | `PK(sap_code, plu)` | `PK(chain, sap_code, plu)` |
| `product_images` | `UNIQUE(sap_code, plu, image_url)` | `UNIQUE(chain, sap_code, plu, image_url)` |
| `enrichment_tasks` | `PK(sap_code, plu)` | `PK(chain, sap_code, plu)` |
| `favorites` | `PK(sap_code, plu)` | `PK(chain, sap_code, plu)` |

SQLite не позволяет `ALTER TABLE ... ADD COLUMN` в PK напрямую:
миграция создаёт новые таблицы, копирует строки с `chain='5ka'`, переименовывает
их и пересоздаёт индексы. Внутри одной транзакции, с предварительным бэкапом.

## 2. Транзакция миграции

1. Проверить `schema_meta.schema_version = 9`, иначе abort.
2. `BEGIN IMMEDIATE`.
3. Создать `*_v10` копии таблиц с расширенными ключами.
4. `INSERT INTO ... SELECT ..., '5ka' FROM ...`.
5. `DROP TABLE` старых, `ALTER TABLE ... RENAME TO`.
6. Пересоздать индексы (`idx_history_product_time`, … с `chain`).
7. Записать `schema_meta.schema_version = '10'`, `COMMIT`.
8. Проверить counts: products 4833, price_history 5580, product_details 215,
   product_images 3285 (на момент проверки 2026-11-24).

## 3. Происхождение и полнота

- `source_chain`, `sap_code`, `source_url`, `source_hash`, `collection_mode`,
  `captured_at`, `imported_at` — обязательные поля выгрузки.
- `field_provenance` (новое): `catalog` / `details` / `price_history` — откуда
  получено поле. Хранится в JSON-колонке card metadata.
- Индикатор полноты по 8 полям: название, цена, фото, состав, БЖУ, вес,
  срок хранения, бренд. Считается как «найдено в любом источнике».

## 4. Восстановление качества (S20)

При импорте карточки и в одноразовой миграции:

```text
composition ← regex из details_text: «Состав» … до «Вес|Срок хранения|…»
description ← «Описание» … до «Показать полностью|Состав»
nutrition   ← parse_nutrition_from_details_text(details_text)
```

Результат помечается как «анализ текста состава, не лабораторная проверка».
Проверка противоречий: «150 г» vs «0.15 кг», «250 г» vs «0.3 кг» —
привод к одним единицам, расхождение > 5% → флаг.

## 5. История карточки

`product_detail_history` уже хранит снимки `details_json`; UI показывает
последние 20 и подсвечивает изменения состава/БЖУ/веса/срока/бренда/фото/названия.

## 6. Идемпотентность

См. [CD-ADR-004](../adr/CD-ADR-004-idempotency-and-lineage.md).
`# source_hash + chain + sap_code + plu` — ключ наблюдения.

## 7. Экспорт

Excel/CSV получают колонку «Сеть»; листы по сетям или единый лист с фильтром.
Имена файлов становятся нейтральными (`catalog_v10.xlsx`) с сохранением
старых имён как опции совместимости.
