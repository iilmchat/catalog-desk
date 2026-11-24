# Дизайн V15 — сборщики и адаптеры

**Связанные:** [DESIGN.md](DESIGN.md) · [CD-ADR-001](../adr/CD-ADR-001-dom-only-collection.md) · CD-ADR-003

---

## 1. Конфиг сети

```python
STORES = {
    "5ka": {
        "domain": "5ka.ru",
        "www_domain": "www.5ka.ru",
        "product_url_prefix": "https://5ka.ru/product/",
        "category_url_regex": r"--([A-Za-z0-9]{6,})(?:/|$)",
        "image_hosts": ["catalog-images.x5static.net"],
        "referer": "https://5ka.ru/",
        "source_markers": ["5ka.ru-visible-page-export", ...],
        "filename_prefix": "5ka",
        "expected_sap": ["35XY"],
    },
    "perekrestok": {
        "domain": "www.perekrestok.ru",
        "product_url_prefix": "https://www.perekrestok.ru/product/",
        "filename_prefix": "pr",
        "expected_sap": ["XSEO", ...],
        # остальные поля совпадают с 5ka — это подтверждено тестовой выгрузкой
    },
}
```

## 2. Контракт Export v2

```json
{
  "schema_version": 2,
  "chain": "perekrestok",
  "source": "visible-page-export",
  "collection_mode": "manual-current-page-dom-only",
  "captured_at": "2026-11-24T20:05:47.446Z",
  "source_url": "https://www.perekrestok.ru/catalog/...--268C62016?page=12",
  "store": {"sap_code": "XSEO", "name": null},
  "pagination": {"current_page_number": 12, "max_page_linked": 12},
  "products": [{"plu": "...", "name": "...", "product_url": "...",
                "image_url": "...", "image_alt": "...", "raw_text": "..."}]
}
```

Импортёр читает и v1: если `chain` отсутствует, он выводится из `source`/домена
или берётся из явного `--chain`. Старые файлы остаются рабочими.

## 3. Общие для сетей элементы подтверждены тестом

- `a[href*="/product/"]` и PLU `--(\d+)`;
- цена в `raw_text` как `Цена 645 99 ₽` — `PRICE_RE` распознаёт 432/432;
- старая цена `Старая цена 399 99 ₽`, маркер `-25%`;
- галерея на `catalog-images.x5static.net`, русские метки рекомендаций;
- `localStorage.DeliveryPanelStore` как источник SAP.

## 4. Отличия, требующие адаптера

| Аспект | 5ka | perekrestok |
| :--- | :--- | :--- |
| Домен каталога | `5ka.ru/catalog/...` | `www.perekrestok.ru/catalog/...` |
| URL товара | заканчивается на `/` | без завершающего `/` |
| SAP | из localStorage | из localStorage; detail-букмаркет только fallback prompt |
| Имя файла | `5ka_<SAP>_...` | `pr_<SAP>_...` |
| @match userscript | `https://5ka.ru/*` | `https://www.perekrestok.ru/*` |

## 5. Validation и тесты

- Unit-тесты на JS: PLU, категория, цена, composition/nutrition из фикстур текста.
- Контрактные тесты JSON v1/v2 на импортёре.
- Тест «один PLU, две сети»: записи не перезаписывают друг друга.
- Тест CAPTCHA:Userscript шлёт `/api/batch/fail`, пакет останавливается.

## 6. Запреты

Не использовать внутренние API, не читать cookies, не обходить CAPTCHA,
не копировать весь проект под магазин (CD-ADR-003).
