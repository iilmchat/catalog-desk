# Документация Catalog Desk V14.x

**Обновлено:** 2026-11-24
**Назначение:** единая точка входа. Выберите роль и переходите по ссылке.

---

## С чего начать

| Роль | Куда идти |
| :--- | :--- |
| **Впервые в проекте** | [GET_STARTED.md](../GET_STARTED.md) — установка и первый запуск · [README.md](../README.md) — обзор |
| **Пользователь** | [user_guide/USER_GUIDE.md](user_guide/USER_GUIDE.md) — интерфейс, сбор, импорт, обогащение |
| **Тестировщик** | [qa/MANUAL_TESTING_GUIDE.md](qa/MANUAL_TESTING_GUIDE.md) — ручная приёмка |
| **Разработчик** | [development/RULES.md](development/RULES.md) → [development/ARCHITECTURE.md](development/ARCHITECTURE.md) → [development/RELEASES.md](development/RELEASES.md) |
| **Дизайн V15** | [development/v15/DESIGN.md](development/v15/DESIGN.md) и три документа-спецификации рядом |

---

## Структура

```text
docs/
├── README.md                    ← этот хаб
├── KNOWN_ISSUES.md              ← реестр проблем (CD-XXX)
├── TODO_KI.md                   ← план устранения CD
├── TODO_SPRINTS.md              ← разбивка на шаги/спринты S15–S22
├── user_guide/
│   └── USER_GUIDE.md            ← пользовательские сценарии
├── qa/
│   └── MANUAL_TESTING_GUIDE.md  ← ручная приёмка
└── development/
    ├── RULES.md                 ← правила разработки
    ├── ARCHITECTURE.md          ← слои, БД, потоки
    ├── RELEASES.md              ← процесс релиза
    ├── adr/                     ← архитектурные решения (CD-ADR-001…006)
    ├── reports/                 ← отчёты по шагам
    └── v15/                     ← дизайн многомагазинной версии
        ├── DESIGN.md
        ├── DESIGN_PARSERS.md
        ├── DESIGN_DATA.md
        └── DESIGN_UI.md
```

---

## Пользователям

- [Руководство пользователя](user_guide/USER_GUIDE.md) — как собирать и импортировать данные, работать с очередью.
- [Реестр известных проблем](KNOWN_ISSUES.md) — формат `CD-XXX`.
- [История версий](../CHANGELOG.md) · [Установка](../GET_STARTED.md).

## Тестировщикам

- [Чек-лист ручной приёмки](qa/MANUAL_TESTING_GUIDE.md) — smoke, regression, проверка импорта Перекрёстка.
- Автотесты: `python -m unittest discover -p "test_*.py"` (Contributing: тесты не ходят в сеть).
- Найденный дефект → запись `CD-XXX` в [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Разработчикам

- [RULES.md](development/RULES.md) — обязательные правила (версия 0.1).
- [ARCHITECTURE.md](development/ARCHITECTURE.md) — текущая и целевая архитектура.
- [ADR-индекс](development/adr/README.md) — архитектурные решения.
- [TODO.md](../TODO.md) — master-план по шагам; [TODO_SPRINTS.md](TODO_SPRINTS.md) — разбивка.

---

## Правила работы с документацией

1. Документация обновляется **в том же коммите**, что и код (RULES § 2.1).
2. `CHANGELOG.md` — Keep a Changelog; рабочая секция `[Unreleased]`.
3. Ссылки — только относительные.
4. Новый дефект/ограничение → `CD-XXX` в [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
5. Отчёт по шагу — по шаблону из [development/reports/README.md](development/reports/README.md).
