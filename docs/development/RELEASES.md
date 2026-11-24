# Процесс релиза Catalog Desk

**Версии:** `V14.0` (текущая), `V15.0` (в работе).
**Правила:** [RULES.md](RULES.md) · **План:** [TODO.md](../../TODO.md)

---

## 1. Версионирование

- Формат `V<major>.<minor>`; major — смена модели данных или появление сети,
  minor — обратно совместимые функции.
- Единственный источник версии — `VERSION.txt` и `CHANGELOG.md`; сервер отдаёт
  ту же строку в `/api/health`.
- Рабочие записи — в `[Unreleased]`; при релизе секция переименуется в `[V15.0] — дата`.

## 2. Чек-лист релиза

1. [ ] Все шаги TODO для версии отмечены выполненными.
2. [ ] `python -m unittest discover -p "test_*.py"` — зелёный.
3. [ ] Smoke по [MANUAL_TESTING_GUIDE.md](../qa/MANUAL_TESTING_GUIDE.md) — пройден.
4. [ ] Миграция применена к копии боевой базы; оригинал не изменён до проверки.
5. [ ] Отчёт по шагу добавлен в `reports/v15/`.
6. [ ] `README.md`, `GET_STARTED.md`, `ARCHITECTURE.md`, `KNOWN_ISSUES.md` актуальны.
7. [ ] `CHANGELOG.md`: `[Unreleased]` → `[Vxx.y] — YYYY-MM-DD`, новая пустая секция.
8. [ ] `VERSION.txt` обновлён; `start_ui_vXX.bat` и папка `ui_vXX` соответствуют версии.
9. [ ] Дистрибутив `pyaterochka_catalog_desk_vXX/` собран и проверен распаковкой в чистую папку.
10. [ ] Коммит с сообщением `chore(release): V15.0`, тег `v15.0`, push.

## 3. Миграции базы

- adдитивные (`ALTER TABLE ... ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`);
- версия схемы хранится в `schema_meta`, одноразовые флаги — в `catalog_ui_meta`;
- перед первой миграцией V14 создавался `*.pre_v14_backup.sqlite3`;
- перед V15 (v9 → v10) обязательна ручная копия базы и проверка counts:
  `products`, `price_history`, `product_details`, `product_images`.

## 4. Откат

1. Остановить сервер.
2. Вернуть файл базы из бэкапа (и `*.sqlite3-wal`/`*.shm`, если они есть).
3. Verнуть предыдущие `catalog_ui_server_vX.py`, `ui_vX/`, `import_5ka_catalog_vX.py`.
4. Поднять сервер на прежнем порту и проверить `/api/health`.

## 5. История релизов

| Версия | Дата | Тема |
| :--- | :--- | :--- |
| V14.0 | 2026-11-24 | Пакетное обогащение, качество БЖУ и галерей |
| V9 | 2026-11-24 | Накопительная SQLite, история цен, Excel/CSV |
| V7 | 2026-11-24 | DOM-only bookmarklet'ы и первый экспорт |
