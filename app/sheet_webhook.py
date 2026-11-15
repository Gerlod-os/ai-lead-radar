#!/usr/bin/env python3
"""sheet_webhook.py — отправка лидов в Google Таблицу через вебхук Apps Script.

Зачем такой путь, а не Google Sheets API. API требует сервисный аккаунт,
`credentials.json`, библиотеку gspread и включённый Google Cloud — три экрана
настроек и файл с секретом в проекте. Вебхук Apps Script требует одну вставку
скрипта в саму Таблицу и один адрес в .env. Для витрины «за 2 минуты» это
разница между «работает» и «не настроил».

Границы, которые надо знать (проверено по документации 15.11.2026):
  * Адрес вебхука — это право писать в таблицу для любого, кто его узнал.
    Поэтому в теле каждой строки лежит общий SHEET_TOKEN, и скрипт сверяет его.
    Адрес и токен живут ТОЛЬКО в .env, в репозиторий не попадают.
  * В манифесте нужен доступ ANYONE_ANONYMOUS («любой, даже без входа»).
    Просто ANYONE по документации значит «любой вошедший» — POST из Python
    без логина упрётся в страницу авторизации.
  * Apps Script отвечает на POST редиректом 302 на другой домен. Тело при этом
    не «теряется»: doPost уже выполнился на первом запросе, а редирект нужен только
    чтобы забрать ответ. Переигрывать POST на адрес редиректа НЕЛЬЗЯ — Google
    отвечает 405 и строка не уходит (проверено живьём 15.11.2026).
  * Вебхук ОПЦИОНАЛЕН. Не задан адрес — работает только CSV, и это нормальный
    режим, а не ошибка.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import budget
import storage as st
from budget import budget_value, pick_budget

TIMEOUT = 15          # секунд: вебхук — расходная операция, долго ждать нечего

# Пятибалльные «чипы» (выпадающие списки) встречаются в чужих заготовках таблиц.
# Наша оценка 0–10 в такую колонку не влезает — переводим, чтобы не ломать проверку
# значений и не оставлять в колонке пустоту. Соответствие названо явно:
# 9–10 → «5 - Высокая», 8 → «4 - Хорошая», 7 → «3 - Средняя», 6 → «2 - Низкая», ниже → «1 - Спам».
RATING_CHIPS = ((9, "5 - Высокая"), (8, "4 - Хорошая"), (7, "3 - Средняя"), (6, "2 - Низкая"))


def rating_chip(score) -> str:
    """Перевести оценку 0–10 в подпись пятибалльного чипа. Мусор — «3 - Средняя»."""
    try:
        value = int(score)
    except (TypeError, ValueError):
        return "3 - Средняя"
    for threshold, label in RATING_CHIPS:
        if value >= threshold:
            return label
    return "1 - Спам"


def payload(row: dict, token: str) -> dict:
    """Тело запроса сразу под две схемы колонок.

    Наша схема: score / url / budget текстом. Схема чужих заготовок:
    rating / link / status / budget числом. Отправляем оба смысла в совместимом виде:
    лишние поля скрипт просто не читает, а недостающие тихо превращаются в пустую
    колонку или в значение «по умолчанию». Именно так терялась оценка: скрипт читал
    `rating`, а мы присылали `score`.
    """
    return {**row, "token": token,
            "budget": pick_budget(row.get("budget"), row.get("price")),
            "rating": rating_chip(row.get("score")),
            "link": row.get("url") or "",
            "status": row.get("status") or "Новый"}


def parse_answer(answer: str) -> tuple[bool, str]:
    """Понять ответ скрипта. Вернуть (принято, причина отказа).

    Форматов два, и оба встречаются:
      * наш `.gs` отвечает текстом `ok`;
      * чужие заготовки отвечают JSON `{"ok":true,"fileUrl":""}` или
        `{"ok":false,"error":"Invalid token"}`.
    Считать «ok» только текстом нельзя: JSON-ответ выглядел «непонятным», строка
    не помечалась отправленной и уходила заново каждый цикл.
    """
    body = (answer or "").strip()
    lower = body.lower()
    if lower.startswith("ok"):
        return True, ""
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            return False, f"непонятный ответ: {body[:80]}"
        if isinstance(data, dict) and data.get("ok") is True:
            return True, ""
        error = data.get("error") if isinstance(data, dict) else None
        return False, f"скрипт отказал: {error or body[:80]}"
    if "deny" in lower:
        return False, "скрипт отверг токен (deny) — сверь SHEET_TOKEN в .env и в .gs"
    return False, f"непонятный ответ: {body[:80] or 'пусто'}"


def push_row(url: str, token: str, row: dict) -> tuple[bool, str]:
    """Отправить одну строку. Вернуть (принято, причина отказа).

    Принято — только когда скрипт подтвердил приём (текстом `ok` или JSON `ok:true`).
    Любой другой ответ, код или обрыв сети считается отказом: строку повторим
    следующим циклом.

    Про редирект — здесь была ошибка, и вот чем она кончилась. Apps Script
    отвечает на POST кодом 302 на другой домен (`script.googleusercontent.com`).
    Первый POST при этом УЖЕ выполнил doPost; 302 — это только способ забрать
    ответ. Стандартный urllib на 302 меняет метод на GET — и это правильно.
    А вот «умный» обработчик, который переигрывал POST на адрес редиректа,
    получал от Google `HTTP 405 Method Not Allowed`, и ни одна строка не уходила.
    Проверено 15.11.2026 на живом развёртывании: 405 и ноль строк. Поэтому здесь
    обычный urlopen, без самодеятельности.
    """
    body = json.dumps(payload(row, token), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            answer = response.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"сеть: {error}"
    return parse_answer(answer)


def push_pending(conn, env: dict, min_score: int) -> tuple[int, str]:
    """Отправить все лиды, которых ещё нет в Таблице. Вернуть (сколько ушло, причина).

    Функция НИКОГДА не бросает исключение: отказ вебхука не должен ронять цикл
    сбора заказов. Причина возвращается текстом — её печатает monitor.py.
    """
    url = (env.get("GOOGLE_SHEET_WEBHOOK_URL") or "").strip()
    token = (env.get("SHEET_TOKEN") or "").strip()
    if not url:
        return 0, "адрес вебхука не задан — работает только CSV"
    if not token:
        return 0, "нет SHEET_TOKEN — не отправляю (без токена вебхук открыт любому)"
    rows = st.sheet_pending(conn, min_score)
    if not rows:
        return 0, ""
    posted = 0
    for row in rows:
        ok, why = push_row(url, token, row)
        if not ok:
            return posted, why
        st.mark_sheet_sent(conn, row["id"])
        posted += 1
    return posted, ""
