"""Реестр магазинов Catalog Desk.

Единственный источник доменов, префиксов URL, хостов фото, разрешённых SAP,
маркеров.source и путей данных. Модуль не зависит от сервера и импортёра,
поэтому его можно использовать в тестах и в сборщиках.

См. CD-ADR-003 (конфиг вместо копирования проекта).
"""

from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

STORES: dict[str, dict] = {
    "5ka": {
        "title": "Пятёрочка",
        "domain": "5ka.ru",
        "www_domain": "www.5ka.ru",
        "match_domains": ["5ka.ru", "www.5ka.ru"],
        "product_url_prefix": "https://5ka.ru/product/",
        "catalog_url_prefix": "https://5ka.ru/catalog/",
        "category_id_regex": r"--([A-Za-z0-9]{6,})(?:/|$)",
        "image_hosts": ["catalog-images.x5static.net"],
        "referer": "https://5ka.ru/",
        "source_markers": {
            "5ka.ru-visible-category-links",
            "5ka.ru-visible-page-export",
            "5ka.ru-visible-product-details-export",
            "5ka.ru-visible-product-detail-export",
        },
        "filename_prefix": "5ka",
        "sap_codes": ["35XY"],
        "default_sap": "35XY",
        "paths": {
            "exports": "5ka_exports",
            "categories": "5ka_category_lists",
            "details": "5ka_product_details",
            "photos": "photos5ka",
        },
    },
    "perekrestok": {
        "title": "Перекрёсток",
        "domain": "www.perekrestok.ru",
        "www_domain": "www.perekrestok.ru",
        "match_domains": ["perekrestok.ru", "www.perekrestok.ru"],
        "product_url_prefix": "https://www.perekrestok.ru/product/",
        "catalog_url_prefix": "https://www.perekrestok.ru/catalog/",
        "category_id_regex": r"--([A-Za-z0-9]{6,})(?:/|$)",
        "image_hosts": ["catalog-images.x5static.net"],
        "referer": "https://www.perekrestok.ru/",
        "source_markers": {
            "5ka.ru-visible-category-links",
            "5ka.ru-visible-page-export",
            "5ka.ru-visible-product-details-export",
            "5ka.ru-visible-product-detail-export",
            "perekrestok.ru-visible-category-links",
            "perekrestok.ru-visible-page-export",
            "perekrestok.ru-visible-product-details-export",
            "visible-category-links",
            "visible-page-export",
            "visible-product-details-export",
        },
        "filename_prefix": "pr",
        "sap_codes": ["XSEO", "47XS"],
        "default_sap": "XSEO",
        "paths": {
            "exports": "pr_exports",
            "categories": "pr_category_lists",
            "details": "pr_product_details",
            "photos": "photospr",
        },
    },
}

DEFAULT_CHAIN = "5ka"


def get_store(chain: str) -> dict:
    """Вернуть конфиг сети; неизвестная сеть → ValueError."""
    try:
        return STORES[chain]
    except KeyError as exc:
        raise ValueError(f"Неизвестная сеть: {chain!r}") from exc


def normalize_chain(value: str | None) -> str:
    """Привести параметр chain к известному значению; None → сеть по умолчанию."""
    if not value:
        return DEFAULT_CHAIN
    chain = str(value).strip().lower()
    if chain not in STORES:
        raise ValueError(f"Неизвестная сеть: {chain!r}")
    return chain


def chain_from_url(url: str | None) -> str:
    """Определить сеть по URL карточки/каталога; неизвестный домен → сеть по умолчанию."""
    text = str(url or "").lower()
    for chain, cfg in STORES.items():
        for domain in cfg["match_domains"]:
            if domain in text:
                return chain
    return DEFAULT_CHAIN


def chain_from_source(source: str | None) -> str | None:
    """Вывести сеть из маркера source; для нейтральных маркеров — None."""
    text = str(source or "").lower()
    if text.startswith("perekrestok"):
        return "perekrestok"
    if text.startswith("5ka"):
        return "5ka"
    return None


def allowed_sap(chain: str) -> list[str]:
    """Список разрешённых SAP-кодов сети."""
    return list(get_store(chain)["sap_codes"])


def path_for(chain: str, kind: str) -> Path:
    """Путь к папке данных сети: exports/categories/details/photos."""
    cfg = get_store(chain)
    try:
        return BASE_DIR / cfg["paths"][kind]
    except KeyError as exc:
        raise ValueError(f"Неизвестный тип папки: {kind!r}") from exc


def all_sources() -> set[str]:
    """Объединение маркеров source всех сетей (для валидации загрузки)."""
    result: set[str] = set()
    for cfg in STORES.values():
        result.update(cfg["source_markers"])
    return result


def product_url_prefix(chain: str) -> str:
    """Префикс URL карточки товара сети."""
    return str(get_store(chain)["product_url_prefix"])
