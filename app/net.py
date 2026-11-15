#!/usr/bin/env python3
"""net.py — сеть, которая знает про прокси. Одно место на весь проект.

Зачем это отдельным файлом. С машины владельца `api.telegram.org` напрямую
не отвечает: TLS-рукопожатие висит до таймаута (замер 15.11.2026 — блок по имени,
как у `t.me`). Через тот же локальный HTTP-прокси, которым ходит модель, запрос
проходит за секунды. Раньше прокси был только у модели, а Telegram ходил напрямую —
и карточки молча не уходили.

Правило: адрес прокси НИКОГДА не пишется в коде. Он приходит из .env:
  LLM_PROXY_URL — для модели (opencode_cli)
  TG_PROXY_URL  — для Telegram (и для бота, и для карточек)
Если обе пусты — работаем напрямую (так живут машины без блокировок).
"""
from __future__ import annotations

import urllib.parse
import urllib.request


def proxy_of(env: dict, *keys: str) -> str:
    """Первый непустой адрес прокси из перечисленных ключей. Пусто — напрямую."""
    for key in keys:
        value = str((env or {}).get(key) or "").strip()
        if value:
            return value
    return ""


def opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """Открывалка для urllib: с прокси или без.

    Только http:// — socks5 не поддерживается ни urllib, ни HTTP-стеком клиента
    модели (проверено 15.11.2026). Если в .env окажется socks5, запрос упадёт
    с понятной ошибкой «unknown url type», а не молча.
    """
    if not proxy:
        return urllib.request.build_opener()
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return urllib.request.build_opener(handler)


def split_proxy(proxy: str) -> tuple[str, int]:
    """Разобрать адрес прокси на (хост, порт). Для http.client, где нужен туннель."""
    parsed = urllib.parse.urlparse(proxy)
    if parsed.scheme not in ("http", ""):
        raise ValueError(f"прокси должен быть http://, получено {parsed.scheme!r}")
    return parsed.hostname or "127.0.0.1", parsed.port or 80
