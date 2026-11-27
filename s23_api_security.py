"""Strict request validation for the loopback-only Catalog Desk HTTP API."""
from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit


_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
_ALLOWED_PAGE_ORIGINS = {"https://5ka.ru", "https://www.5ka.ru", "https://perekrestok.ru", "https://www.perekrestok.ru"}


def validate_local_request(client_address, host_header: str | None, origin_header: str | None, expected_port: int | None = None) -> None:
    """Reject non-loopback peers and browser requests not addressed to this local origin."""
    try:
        peer = ipaddress.ip_address(str(client_address[0]))
    except (ValueError, TypeError, IndexError):
        raise ValueError("Недопустимый адрес клиента")
    if not peer.is_loopback:
        raise ValueError("Локальный API принимает запросы только с этого компьютера")

    host_value = str(host_header or "").strip()
    if not host_value or any(ch in host_value for ch in "/\\\\@?#"):
        raise ValueError("Недопустимый Host")
    parsed_host = urlsplit("//" + host_value)
    hostname = (parsed_host.hostname or "").lower()
    try:
        port = parsed_host.port
    except ValueError:
        raise ValueError("Недопустимый порт Host")
    if hostname not in _ALLOWED_HOSTS or (port is not None and not 1 <= port <= 65535):
        raise ValueError("Host не относится к локальному интерфейсу")
    if expected_port is not None and port != int(expected_port):
        raise ValueError("Host указывает на другой порт")

    origin_value = str(origin_header or "").strip()
    if origin_value:
        origin = urlsplit(origin_value)
        local_origin = (
            origin.scheme.lower() == "http" and
            (origin.hostname or "").lower() in _ALLOWED_HOSTS and
            origin.netloc.lower() == host_value.lower()
        )
        retailer_origin = origin_value.rstrip("/").lower() in _ALLOWED_PAGE_ORIGINS
        if (not (local_origin or retailer_origin) or
                origin.username is not None or origin.password is not None or
                origin.path not in ("", "/") or origin.query or origin.fragment):
            raise ValueError("Origin не разрешён для локального интерфейса")


def parse_json_object(content_type: str | None, content_length: str | None,
                      body: bytes, max_bytes: int = 32 * 1024 * 1024) -> dict:
    """Parse a bounded JSON object; reject ambiguous or truncated HTTP bodies."""
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ValueError("Ожидается Content-Type: application/json")
    try:
        length = int(str(content_length or ""))
    except (TypeError, ValueError):
        raise ValueError("Некорректный Content-Length")
    if length <= 0 or length > max_bytes or len(body) != length:
        raise ValueError("Некорректный размер запроса")
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Некорректный JSON UTF-8") from exc
    if not isinstance(value, dict):
        raise ValueError("Тело запроса должно быть JSON-объектом")
    return value
