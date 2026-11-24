# Архитектурные решения (ADR)

| ADR | Решение | Статус |
| :--- | :--- | :--- |
| [CD-ADR-001](CD-ADR-001-dom-only-collection.md) | Сбор только из DOM открытой страницы | Accepted |
| [CD-ADR-002](CD-ADR-002-chain-in-data-model.md) | `chain` в модели данных, одна БД на все сети | Accepted (проект V15) |
| [CD-ADR-003](CD-ADR-003-store-config-not-copy.md) | Конфиг магазинов вместо копирования проекта | Accepted (проект V15) |
| [CD-ADR-004](CD-ADR-004-idempotency-and-lineage.md) | Идемпотентность через `source_hash`, разделение времён | Accepted |
| [CD-ADR-005](CD-ADR-005-captcha-stop.md) | CAPTCHA — остановка, обход запрещён | Accepted |
| [CD-ADR-006](CD-ADR-006-server-side-quality-repair.md) | Восстановление состава/БЖУ на сервере | Accepted (проект V15) |

Формат: контекст → решение → последствия. Новое решение — отдельный файл `CD-ADR-XXX-*.md` + строка в этой таблице.
