# Changelog

Все значимые изменения проекта фиксируются в этом файле.
Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/),
версионирование — [Semantic Versioning](https://semver.org/lang/ru/).

## [Unreleased]

### Added
- Документация по образцу PriceScope: `README.md`, `GET_STARTED.md`, `TODO.md`,
  `docs/` (хаб, реестр `CD-XXX`, TODO_KI, TODO_SPRINTS, RULES, ARCHITECTURE,
  RELEASES, ADR, дизайн V15, пользовательское руководство и QA-чеклист).
- Git-репозиторий: `.gitignore`, `.gitattributes`, `.editorconfig` — код и документация
  версионируются, база/выгрузки/фотографии исключены.
- S15: `catalog_config.py` — реестр сетей `5ka`/`perekrestok` (домены, префиксы URL,
  хосты фото, SAP, `source`-маркеры, пути); `catalog_migrations.py` — миграция V9→V10
  с добавлением `chain`, расширением ключей и одноразовым бэкапом; 6 новых тестов
  (`test_catalog_config_v15.py`).

### Changed
- Зафиксирован план V15: многомагазинная модель, конфиг магазинов, импорт
  Перекрёстка, качество данных (состав/БЖУ), аналитика и сравнение.
- `catalog_ui_server_v14.py` и `import_5ka_catalog_v9.py` применяют миграцию V10
  при старте/импорте; `ON CONFLICT` переведены на ключи с `chain`.
- `EXPECTED_SAP` берётся из конфига сети по умолчанию вместо литерала `35XY`.

## [14.0] — 2026-11-24

**Тема:** пакетное обогащение карточек через userscript и контроль качества БЖУ/галерей.

### Added
- `5ka_batch_enrichment_v14.user.js`: последовательное открытие карточек, лимит
  1–200, пауза 12–60 сек, останов при CAPTCHA, продолжение пакета.
- Восстановление БЖУ из `details_text` при обоих порядках («3.5 Белки» и «Белки 3.5»),
  игнорирование знаменателя «на 100 г/мл».
- Очистка галерей от фотографий рекомендательных блоков, отбор по `alt` и позиции.
- Одноразовый бэкап базы перед миграцией: `pyaterochka_catalog_v9.pre_v14_backup.sqlite3`.
- Возврат неполных старых карточек в очередь обогащения.
- Интерфейс: панель пакетного режима, прогресс, кнопки «Начать/Пауза/Продолжить».

### Fixed
- Сдвинутые значения БЖУ в старых снимках исправляются при старте сервера.
- Старые detail-ссылки на рекомендательные фотографии удаляются один раз.

### Known limitations
- Только одна сеть: `SAP 35XY`, домен `5ka.ru`, маркеры `source` с префиксом `5ka`.
- Detail-карточки с другим SAP отклоняются импортом.
- Состав и описание могут отсутствовать, даже если видны на странице (CD-003, CD-005).
- новая сеть требует изменений кода, а не только конфигурации.

## [9.0] — 2026-11-24

**Тема:** накопительная SQLite-база и история цен.

### Added
- `import_5ka_catalog_v9.py`: дедупликация выгрузок по SHA-256, разделение
  `captured_at` и `imported_at`, события первого появления и изменений,
  Excel (9 листов) и CSV.
- Таблицы `products`, `price_history`, `product_categories`, `exports`.

## [7.0] — 2026-11-24

**Тема:** первые DOM-only bookmarklet'ы и экспорт в Excel.

### Added
- Экспорт категорий, текущей страницы и карточки товара из видимого DOM.
- `import_5ka_exports_v7.py`, `pyaterochka_prices_v7.xlsx/.csv`.

---

[Unreleased]: https://github.com/iilmchat/catalog-desk/compare/main...HEAD
[V14.0]: https://github.com/iilmchat/catalog-desk/releases/tag/v14.0
[V9]: https://github.com/iilmchat/catalog-desk/releases/tag/v9
[V7]: https://github.com/iilmchat/catalog-desk/releases/tag/v7
