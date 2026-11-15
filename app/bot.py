#!/usr/bin/env python3
"""bot.py — админка ai-lead-radar: ОДНА панель, кнопки, управление с телефона.

ПАТТЕРН «ОДНО ОКНО» (решение владельца 18.09.2026):
бот шлёт панель заново В КОНЕЦ ленты и удаляет прежнюю. Нажатие любой кнопки
всегда видно перед глазами, лента чистая. Все тексты — HTML: жирные заголовки,
деньги, кнопки-ссылки; чужой текст экранируется h(), битый HTML не роняет
панель — повтор простым текстом (и в боте, и в карточках monitor.py).

СЛОИСТАЯ СТРУКТУРА (условие: легко убирать/добавлять функции):
  ЧАСТЬ 1 — транспорт: api(), show(), wipe() (НЕ ТРОГАТЬ при адаптации).
  ЧАСТЬ 2 — разделы: render_* + КАРТОЧКА РАЗДЕЛА в SECTIONS.
      Убрать раздел = убрать одну строку из SECTIONS.
      Добавить раздел = новая render_* + одна строка в SECTIONS.
  ЧАСТЬ 3 — кнопки: section_action() — действия разделов (по подписям reply-кнопок).
  ЧАСТЬ 4 — главный цикл (НЕ ТРОГАТЬ при адаптации).

МЕНЮ (reply-кнопки): Статус, Заказы, Статистика, Источники, Фильтры, Архив, Проверить.
Действия разделов — ТОЖЕ reply-кнопки у поля ввода (слова, цена ±1000, вакансии,
вкл/выкл источника, «⬅️ В меню»). Inline-кнопок в чате владельца нет вовсе:
смесь двух видов кнопок и была причиной «кнопки пропали» (урок У57).
Текстом (/status …) — резерв. Принимает только владельца (CHAT_ID из .env).
Запуск: python3 bot.py — висит постоянно (пока компьютер включён).
Только стандартная библиотека Python 3.9+ (проверено на 3.9.6 и 3.14).
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import http.client
import io
import queue
import re
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import html as _html

import llm_filter
import net
import stats
import admin_core as core


def h(text) -> str:
    """HTML-экранирование: весь чужой текст (заголовки, слова, площадки) — только через h().

    Telegram-режим HTML: голый `<` в тексте заказа ломает всю карточку
    (BadRequest: can't parse entities). Экранируем всё, что не наша разметка.
    """
    return _html.escape(str(text), quote=False)

import storage as st
from monitor import load_config, log
from sources import load_env
from stats import collect, render, render_html

APP = Path(__file__).resolve().parent
CONFIG = APP.parent / "configs" / "monitor.json"


# ============================================================
# ЧАСТЬ 1. Транспорт: разговор с Telegram API (не трогать)
# ============================================================

# Схема навигации — решение владельца 15.11.2026 (последнее слово):
# 1) ПАНЕЛЬ ОДНА — сообщение с inline-кнопками. Нажатие правит ЭТО ЖЕ сообщение,
#    прежние панели удаляются (карточки заказов не трогаем).
# 2) Клавиатуры у поля ввода НЕТ вовсе. Так было раньше, и получилось две панели
#    сразу; на переключение «то есть, то нет» — потому что кнопка жила в одном
#    разделе. Владелец: «оставь только панель кнопочную под сообщением, без
#    клавиатуры». Старую клавиатуру снимаем один раз за запуск (clear_keyboard_once).
# 3) Действия — кнопками ВНУТРИ разделов: «Фильтры» (слова, цена, вакансии,
#    порог), «Источники» (вкл/выкл), «Промт» (изменить/сбросить).
# 4) Ввод текста (слово, промт) — вопрос с инлайн-кнопкой «✖️ Отмена»:
#    клавиатуру для этого тоже не показываем.


def build_main_kb() -> dict:
    """Меню у поля ввода: 6 разделов + «Проверить сейчас» (лаконично, 18.09.2026).

    Никаких действий-дублей в главном меню: правка слов/цены/вакансий — кнопками
    ВНУТРИ раздела «Фильтры», тумблеры источников — внутри «Источники».
    Подписи кнопок статичны (состояние показываем в тексте панели).
    """
    # Набор ровно как в инлайн-панели (просьба владельца 15.11.2026): те же разделы.
    rows = [
        ["📋 Статус", "🆕 Заказы", "📊 Статистика"],
        ["📦 Архив", "📄 Источники", "🔍 Фильтры"],
        ["🤖 Промт"],
        ["🔄 Проверить сейчас"],
    ]
    return {"keyboard": rows, "resize_keyboard": True}


MAIN_KB = build_main_kb()

# Инлайн-панель: кнопки живут В СООБЩЕНИИ, а не у поля ввода (просьба владельца 15.11.2026).
# Reply-клавиатура остаётся как резерв: на неё по-прежнему приходят тексты слов и промта.
INLINE_MAIN = [
    [("📋 Статус", "s:status"), ("🆕 Заказы", "s:orders"), ("📊 Статистика", "s:stats")],
    [("📦 Архив", "s:archive"), ("📄 Источники", "s:sources")],
    [("🔍 Фильтры", "s:filters"), ("🤖 Промт", "s:prompt")],
    [("🔄 Проверить сейчас", "a:run")],
]
INLINE_BACK = [[("⬅️ В меню", "s:home")]]
PROMPT_ROWS = [[("✏️ Изменить", "a:pr-edit"), ("↩️ Стандартный", "a:pr-reset")],
               [("⬅️ В меню", "s:home")]]
PROMPT_EDIT = "\u270f\ufe0f Изменить промт"   # ✏️ Изменить промт
PROMPT_RESET = "\u21a9\ufe0f Стандартный"     # ↩️ Стандартный
SECTION_ROWS_REPLY: dict[str, dict] = {}   # у разделов без своей reply-клавиатуры — главное меню
SECTION_BY_CODE = {"status": "📋 Статус", "orders": "🆕 Заказы", "stats": "📊 Статистика",
                   "archive": "📦 Архив", "sources": "📄 Источники", "filters": "🔍 Фильтры",
                   "prompt": "🤖 Промт"}

# Клавиатура раздела «Фильтры»: действия живут внутри раздела и приходят тем же
# способом, что и меню, — reply-кнопками У ПОЛЯ ВВОДА (см. «ПОЧЕМУ ТОЛЬКО
# reply-КНОПКИ» выше). Состояние (цена, вакансии, слова) — В ТЕКСТЕ панели,
# подписи кнопок статичны: так подпись не приходится переписывать на каждом шаге.
# Подписи кнопок, которые ОБЯЗАНЫ совпадать в клавиатуре и в обработчике
# нажатия, пишем \u-кодами: эмодзи при правке файла иногда «теряется», а коды —
# чистый ASCII, совпадение гарантировано. Заодно это единая точка правды: подпись
# меняется здесь, а не в трёх местах (клавиатура, маршрут, текст панели).
BACK_LABEL = "\u2b05\ufe0f В меню"                       # <- В меню
SRC_PREFIX = "Источник:"                                # Источник: FL.ru
SRC_ON, SRC_OFF = "\U0001f7e2", "\u26aa\ufe0f"          # вкл / выкл
WORD_ADD, WORD_DEL = "\u2795 Слово", "\u2796 Слово"     # Слово / Слово
PRICE_UP, PRICE_DOWN = "+1000 \u20bd", "\u22121000 \u20bd"    # +1000 / -1000 руб
VAC_LABEL = "\U0001f4bc Вакансии"                       # Вакансии
CANCEL_LABEL = "\u2716\ufe0f Отмена"                    # Отмена
CHECK_LABEL = "\U0001f504 Проверить сейчас"              # Проверить сейчас
SEC_FILTERS = "\U0001f50d Фильтры"                      # ключ раздела в SECTIONS
SEC_SOURCES = "\U0001f4c4 Источники"                    # ключ раздела в SECTIONS

# Порог ИИ и переключение админки — тоже кнопки, и тоже у поля ввода.
# Подписи собраны из кода символов, как и остальные: правка эмодзи руками их ломает.
SCORE_LABELS = {f"\U0001f916 Порог {n}": n for n in (6, 7, 8)}   # «🤖 Порог 6/7/8»
UI_LABEL = "\U0001f5a5 Сменить админку"                 # 🖥 Сменить админку

FILTERS_KB = {"keyboard": [
    [WORD_ADD, WORD_DEL],
    [PRICE_DOWN, PRICE_UP],
    [VAC_LABEL],
    list(SCORE_LABELS),
    [UI_LABEL],
    [BACK_LABEL],
], "resize_keyboard": True}

PROMPT_KB = {"keyboard": [[CANCEL_LABEL]], "resize_keyboard": True}
# Клавиатура раздела «Промт» для вида «у поля ввода»: те же две кнопки, что инлайн.
PROMPT_KB_REPLY = {"keyboard": [[PROMPT_EDIT, PROMPT_RESET], [BACK_LABEL]],
                   "resize_keyboard": True}


HOME_TEXT = ("<b>⚙️ Админка радара</b>\n\n"
             "Кнопки меню — у поля ввода. Каждое нажатие присылает свежую панель, "
             "прежняя удаляется.\n"
             "В «🔍 Фильтры» и «📄 Источники» кнопки правки — тоже у поля ввода, "
             "«⬅️ В меню» возвращает главное меню.\n"
             "Сверху в ленту падают карточки новых заказов.")

PANEL_ID: dict[int, list[int]] = {}
PENDING: dict[int, dict] = {}   # chat_id -> {"mode": "add"/"del", "prompt_id": int}: жду слово
STUCK_KEEP = 3   # сколько неудаляемых панелей помним (старше 48 ч Telegram не удаляет никогда)

PANEL_FILE = APP.parent / "volume" / "panel.json"  # id панели переживает перезапуск


def atomic_write_text(path: Path, text: str) -> None:
    """Записать текст атомарно: tmp рядом + fsync + замена (тот же паттерн, что в tg-admin).

    Файл либо старый целый, либо новый целый — никогда половинка.
    """
    path = Path(path)
    if path.parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent or "."),
                                    prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def save_config(cfg: dict) -> None:
    """Записать конфиг на диск (кнопки: вкл/выкл источника, слова, мин. цена)."""
    atomic_write_text(CONFIG, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")


def save_panel() -> None:
    """Запомнить id панелей на диск: после перезапуска правим ту же панель, а не шлём вторую.

    Пишем поверх файла, СОХРАНЯЯ прочие поля (например `kb_removed`): раньше
    функция затирала файл целиком, флаг терялся и бот при каждом рестарте слал
    «якорь» с меню заново (найдено 18.09.2026).
    """
    try:
        try:
            state = json.loads(PANEL_FILE.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        state.update({str(c): m for c, m in PANEL_ID.items()})
        atomic_write_text(PANEL_FILE, json.dumps(state))
    except Exception:
        pass


def load_panel() -> None:
    try:
        for chat, mid in json.loads(PANEL_FILE.read_text(encoding="utf-8")).items():
            if isinstance(mid, list):                    # новый формат: список id
                PANEL_ID[int(chat)] = [int(x) for x in mid]
            elif str(mid).lstrip("-").isdigit():         # старый формат: одно число
                PANEL_ID[int(chat)] = [int(mid)]
    except Exception:
        pass


_LOCK_HANDLE = None          # файл singleton-лока (держит flock до конца процесса)


_TLS = threading.local()     # у каждого потока (поллер, воркер) — своё соединение


def _proxy_url() -> str:
    """Адрес прокси для Telegram из .env (TG_PROXY_URL, иначе LLM_PROXY_URL).

    Кэшируем в _TLS: читать файл на каждый вызов незачем, а менять прокси
    на ходу всё равно никто не будет. Пусто — работаем напрямую.
    """
    cached = getattr(_TLS, "proxy", None)
    if cached is None:
        try:
            env = load_env(APP / ".env")
            cached = (env.get("TG_PROXY_URL") or env.get("LLM_PROXY_URL") or "").strip()
        except OSError:
            cached = ""
        _TLS.proxy = cached
    return cached


def _telegram_conn(sock_timeout: int) -> http.client.HTTPSConnection:
    """Постоянное (keep-alive) соединение с Telegram для ЭТОГО потока.

    ЗАМЕРЕНО на нашей сети (прокси): новый TLS-коннект = 0.65 с на вызов,
    переиспользование = 0.25 с. Поэтому соединение держим открытым, а не
    поднимаем заново на каждое нажатие кнопки.

    Про прокси: с машины владельца api.telegram.org напрямую не отвечает —
    TLS-рукопожатие висит до таймаута. При заданном прокси соединение идёт
    туннелем CONNECT через него; без прокси — как раньше, напрямую.
    """
    conn = getattr(_TLS, "conn", None)
    if conn is None:
        proxy = _proxy_url()
        if proxy:
            host, port = net.split_proxy(proxy)
            conn = http.client.HTTPSConnection(host, port, timeout=sock_timeout)
            conn.set_tunnel("api.telegram.org", 443)
        else:
            conn = http.client.HTTPSConnection("api.telegram.org", timeout=sock_timeout)
        _TLS.conn = conn
    conn.timeout = sock_timeout          # у каждого вызова свой лимит ожидания
    return conn


def _drop_conn() -> None:
    """Сбросить соединение потока (после сетевого сбоя) — следующий вызов поднимет новое."""
    conn = getattr(_TLS, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _TLS.conn = None


def _retry_after(payload: bytes) -> int:
    """Сколько секунд просит подождать Telegram (из тела 429-ответа)."""
    try:
        params = json.loads(payload or b"{}").get("parameters", {})
        return max(1, int(params.get("retry_after", 1)))
    except (ValueError, TypeError, AttributeError):
        return 1


def api(token: str, method: str, _sock: int = 45, **params) -> dict:
    """Один вызов Telegram Bot API: постоянное соединение + повтор при 429/сети.

    429: читаем retry_after из тела, спим, повторяем (до 4 попыток).
    Сеть рвётся — backoff 0.3→0.9 с со сбросом соединения.
    Остальное (400/409…) — разбирает вызывающий, сразу raise.
    """
    body = urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{token}/{method}"
    path = f"/bot{token}/{method}"
    head = {"Content-Type": "application/x-www-form-urlencoded"}
    last_error: Exception | None = None
    for attempt in (1, 2, 3, 4):
        conn = _telegram_conn(_sock)
        try:
            # keep-alive: у уже созданного сокета таймаут сам НЕ обновляется —
            # задаём явно, иначе зависшее чтение не падает и бот молчит
            # (найдено 18.09.2026: поллер висел, в логе — ни строчки).
            if conn.sock is not None:
                conn.sock.settimeout(_sock)
            conn.request("POST", path, body=body, headers=head)
            resp = conn.getresponse()
            payload = resp.read()
            if resp.status == 429:
                wait = _retry_after(payload)
                log(f"Telegram просит подождать ({wait} с, попытка {attempt}/4)")
                # Соединение после 429 сбрасываем: иначе битый сокет остаётся в пуле
                # и следующие вызовы падают уже сетевой ошибкой.
                _drop_conn()
                time.sleep(wait)
                if attempt == 4:
                    # Последняя попытка: last_error остался бы None → «raise None»
                    # = TypeError, и цикл опроса падает вместо понятной ошибки.
                    last_error = urllib.error.HTTPError(
                        url, resp.status, "Too Many Requests",
                        resp.headers, io.BytesIO(payload))
                continue
            if resp.status >= 400:
                # отдаём вызывающему в том же виде, что и urllib: HTTPError с телом
                raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                             resp.headers, io.BytesIO(payload))
            return json.loads(payload)
        except urllib.error.HTTPError:
            raise                     # это ОТВЕТ Telegram (400/409…), а не сеть — разбирает вызывающий
        except Exception as error:    # сеть моргнула — ждём дольше с каждой попыткой
            last_error = error
            _drop_conn()
            if attempt < 4:
                time.sleep(0.3 * attempt)
    raise last_error


def cut(text: str, limit: int = 3800) -> str:
    """Обрезать текст до лимита ЦЕЛЫМИ словами/строками (жалоба владельца 18.09.2026:
    «посты криво обрезаются — либо слово, либо середина предложения»).

    Сначала пробуем уложиться целиком; иначе режем по последнему переносу строки,
    затем по последнему пробелу — предложение не рвётся посередине слова.
    """
    if len(text) <= limit:
        return text
    head = text[: limit - 2]
    for cut_at in (head.rfind("\n"), head.rfind(" ")):
        if cut_at > limit // 2:            # обрезка не съедает весь текст
            return text[:cut_at].rstrip(" ,;:-—·") + " …"
    return head.rstrip() + " …"


def show(token: str, chat: int, text: str, kb: dict | None = None) -> None:
    """Показать панель: ВСЕГДА новое сообщение, прежние админ-панели удаляем.

    Решение владельца 18.09.2026: «пусть каждый раз создаёт сообщение, а все
    остальные удаляет (только админскую панель, не посты)». Карточки заказов
    не трогаем: в PANEL_ID живут только id панелей. Порядок: сначала шлём
    новую (не вышло — старые остаются), затем чистим прежние. kb — только
    служебный remove_keyboard; обычная панель кнопок клавиатуру не ставит.
    Битый HTML → повтор простым текстом.
    """
    text = cut(text)
    old_ids = PANEL_ID.get(chat, [])
    # Клавиатуру у поля ввода не показываем ВООБЩЕ (решение владельца 15.11.2026):
    # панель одна и живёт под сообщением. kb передают только служебные вызовы,
    # которым нужен именно remove_keyboard.
    extra = {"reply_markup": json.dumps(kb, ensure_ascii=False)} if kb else {}
    try:
        res = api(token, "sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                  disable_web_page_preview="true", **extra)
    except urllib.error.HTTPError as error:
        if "can't parse entities" not in error.read().decode("utf-8", "ignore"):
            log(f"панель: {error}")
            return
        try:    # битый HTML → запасной путь без тегов
            res = api(token, "sendMessage", chat_id=chat,
                      text=re.sub(r"</?[biu][^>]*>", "", text),
                      disable_web_page_preview="true", **extra)
        except Exception as fallback_error:
            log(f"панель (запасной путь): {fallback_error}")
            return
    except Exception as error:
        log(f"панель: {error}")
        return
    new_id = res["result"]["message_id"]
    PANEL_ID[chat] = [new_id]
    save_panel()          # id записан ДО чистки: падение процесса не оставит «ничейную» панель
    stuck = [mid for mid in old_ids if mid != new_id and not wipe(token, chat, mid)]
    if stuck:
        # Остались только те, где «сорвалась сеть» (wipe вернул False): их стоит
        # повторить при следующей перерисовке. Неудаляемые навсегда (панель старше
        # 48 ч) сюда НЕ попадают, поэтому список не растёт — а раньше в panel.json
        # копились мёртвые id, и бот тратил на каждое нажатие 3 с (жалоба владельца
        # 18.09.2026 «долго работает»; в логе — «медленная обработка: 3.3 с»).
        PANEL_ID[chat] = [new_id] + stuck[:STUCK_KEEP]
        save_panel()


# Так Telegram отвечает, когда удалять уже нечего: сообщения нет или оно старше
# 48 часов (свои сообщения Telegram удаляет только первые двое суток). Такие id
# больше НЕ запоминаем: повторять вызов бессмысленно, а именно из-за повторов бот
# и «долго работал» (в panel.json накопилось 28 старых id → 28 вызовов удаления на
# каждое нажатие, лог 18.09.2026: «медленная обработка: 3.7 с»).
GONE_MARKS = ("message to delete not found", "message can't be deleted",
              "message identifier is not specified", "message is not found")

WIPE_TIMEOUT = 15       # таймаут удаления: расходная операция, долго ждать нечего


def wipe(token: str, chat: int, message_id: int, _sock: int = WIPE_TIMEOUT) -> bool:
    """Убрать сообщение — своё (прежняя панель) или владельца (нажатие кнопки).

    True — удалять больше нечего (удалено, уже удалено, или Telegram не отдаёт его
    никогда); False — сорвалось по сети, стоит попробовать позже. Вызывающему
    (show) второе важно: только такие id он держит, чтобы повторить.

    Таймаут короче обычного (WIPE_TIMEOUT): удаление — вещь расходная, из-за него
    не ждём ответа больше 15 с. Плюс фоновые потоки не-daemon, и на выходе из
    процесса Python их дожидается — длинный таймаут затянул бы и остановку бота.
    """
    try:
        api(token, "deleteMessage", chat_id=chat, message_id=message_id, _sock=_sock)
        return True
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "ignore").lower()
        return any(mark in detail for mark in GONE_MARKS)      # False → повторим позже
    except Exception:
        return False


KEYBOARD_CLEARED: set[int] = set()   # в каких чатах клавиатуру уже сняли (один раз за запуск)


def drop_reply_keyboard(token: str, chat: int) -> None:
    """Снять клавиатуру у поля ввода, если она осталась от прошлой версии бота.

    Панель теперь одна — сообщение с кнопками. Но у владельца в чате висит
    клавиатура от прежней схемы, и сама она не исчезнет: её надо снять явно.
    """
    try:
        api(token, "sendMessage", chat_id=chat, text="\u2063",
            reply_markup=json.dumps({"remove_keyboard": True}))
    except Exception:
        pass


def clear_keyboard_once(token: str, chat: int) -> None:
    """Снять клавиатуру один раз за запуск, а не на каждую перерисовку панели."""
    if chat in KEYBOARD_CLEARED:
        return
    KEYBOARD_CLEARED.add(chat)
    drop_reply_keyboard(token, chat)


def show_home(token: str, chat: int, message_id: int | None = None) -> None:
    """Главное меню: сводка статуса + инлайн-кнопки прямо в сообщении."""
    conn = st.connect()
    try:
        text = render_section_text(conn, "📋 Статус")
    finally:
        conn.close()
    clear_keyboard_once(token, chat)
    show_inline(token, chat, text, INLINE_MAIN, message_id)


def show_text_screen(token: str, chat: int, text: str) -> None:
    """Экран-сообщение (результат проверки, подсказка) — та же панель."""
    show(token, chat, text)


# ============================================================
# ЧАСТЬ 2. Разделы: каждый = render_* + КАРТОЧКА в SECTIONS.
# Убрать раздел: удалить его строку в SECTIONS (код можно оставить).
# Добавить раздел: скопировать любой render_*, вписать в SECTIONS.
# Формат render: (текст, клавиатура у поля ввода или None → главное меню).
# Ключ в SECTIONS ДОЛЖЕН совпадать с текстом reply-кнопки в MAIN_KB.
# ============================================================

def render_status(conn) -> tuple[str, None]:
    """Здоровье источников: последний прогон каждого (OK/ошибка, счётчики, молчание)."""
    cfg = load_config(CONFIG)
    stale_hours = int(cfg.get("stale_hours", 2) or 0)   # источник молчит дольше — ⚠️
    lines = ["<b>🏥 Источники</b>", ""]
    for src in cfg["sources"]:
        row = conn.execute(
            "SELECT ts, ok, error, items, new_items FROM runs WHERE source = ? ORDER BY ts DESC LIMIT 1",
            (src["name"],),
        ).fetchone()
        enabled = "🟢" if src.get("enabled", True) else "⚪️"
        if row:
            ts, ok, error, items, new = row
            state = "OK" if ok else f"ОШИБКА: {h(error)}"
            mark = ""
            if src.get("enabled", True):
                try:
                    age_h = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(ts)).total_seconds() / 3600
                    if age_h >= stale_hours:
                        mark = f" ⚠️ молчит {age_h:.0f} ч"
                except ValueError:
                    pass
            lines.append(
                f"{enabled} <b>{h(src['name'])}</b> — {state}\n"
                f"    <i>{items} поз · +{new} новых · {ts[11:16]} UTC{mark}</i>"
            )
        else:
            lines.append(f"{enabled} <b>{h(src['name'])}</b> — ещё не проверялся")
    act = conn.execute("SELECT COUNT(*) FROM orders WHERE status='active' AND kind='order'").fetchone()[0]
    vac = conn.execute("SELECT COUNT(*) FROM orders WHERE status='active' AND kind='vacancy'").fetchone()[0]
    return "\n".join(lines) + f"\n\n📦 В банке: <b>{act}</b> заказов · 💼 <b>{vac}</b> вакансий", None


def render_recent(conn) -> tuple[str, None]:
    """Прошедшие фильтр (notified=1): в чат прилетают только НОВЫЕ из этого списка."""
    show_vac = load_config(CONFIG).get("show_vacancies", False)
    kind_where = "" if show_vac else " AND kind='order'"
    rows = conn.execute(
        f"SELECT title, url, price, kind FROM orders WHERE status='active' AND notified=1{kind_where} "
        "ORDER BY COALESCE(published, first_seen) DESC LIMIT 10"
    ).fetchall()
    lines = ["<b>🆕 Последние по фильтру</b>",
             "<i>в чат прилетают только НОВЫЕ из этого списка</i>", ""]
    mark = {"order": "🚨 ", "vacancy": "💼 "}
    for i, (title, url, price, kind) in enumerate(rows, 1):
        money = f' · <b>~{price:,} ₽</b>'.replace(",", " ") if price else ""
        link = f'\n   <a href="{_html.escape(url, quote=True)}">🔗 открыть</a>' if url else ""
        lines.append(f'{i}. {mark.get(kind, "")}<b>{h(cut(title or "—", 80))}</b>{money}{link}')
    skipped = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE status='active' AND notified=0{kind_where}"
    ).fetchone()[0]
    if skipped:
        lines.append(f"\n<i>ℹ️ В банке ещё {skipped} активных мимо фильтра — бот их не присылает.</i>")
    if not rows and not skipped:
        return "🆕 <i>Пока пусто — нажми «🔄 Проверить сейчас».</i>", None
    return "\n".join(lines), None


def render_statistics(conn) -> tuple[str, None]:
    """Сводка банка: площадки, цены, живость источников (HTML-версия без полосок)."""
    return render_html(collect(conn)), None


def build_sources_kb(cfg: dict) -> dict:
    """Клавиатура раздела «Источники»: по кнопке на источник + «⬅️ В меню».

    Подпись несёт состояние (🟢/⚪️) — его видно прямо на кнопке у поля ввода.
    Имя источника в подписи идёт после слова «Источник:» — по нему нажатие
    узнаёт handle_reply, поэтому формат подписи менять нельзя.
    """
    rows = []
    for src in cfg.get("sources", []):
        mark = SRC_ON if src.get("enabled", True) else SRC_OFF
        rows.append([f"{mark} {SRC_PREFIX} {src['name']}"])
    rows.append([BACK_LABEL])
    return {"keyboard": rows, "resize_keyboard": True}


def render_sources(conn) -> tuple[str, dict]:
    """Источники: кто жив, кто выключен; вкл/выкл — кнопкой у поля ввода."""
    cfg = load_config(CONFIG)
    del conn  # разделу база не нужна
    lines = ["<b>📄 Источники</b>\n"]
    for src in cfg["sources"]:
        on = src.get("enabled", True)
        lines.append(f"{SRC_ON + ' вкл' if on else SRC_OFF + ' выкл'} · "
                     f"<b>{h(src['name'])}</b> — {h(src.get('type', '?'))}")
    lines.append("\n<i>Кнопки у поля ввода переключают источник (панель обновится "
                 "сразу), «⬅️ В меню» вернёт главное меню.</i>")
    return "\n".join(lines), build_sources_kb(cfg)


def render_filters(conn) -> tuple[str, dict]:
    """Фильтры: слова, стоп-слова, мин. цена, порог ИИ, вид админки; правка — кнопками."""
    cfg = load_config(CONFIG)
    del conn  # разделу база не нужна
    score = core.get_min_score(cfg)
    ui = core.get_ui(cfg)
    text = (
        "<b>🔍 Фильтры</b>\n\n"
        f"<b>Слова</b> (ловим по началу слова):\n{h(', '.join(cfg.get('keywords', [])) or '—')}\n\n"
        f"<b>Стоп-слова</b>: {h(', '.join(cfg.get('exclude_words', [])[:5]))}…\n"
        f"<b>Мин. цена</b>: <b>{cfg.get('min_price', 0)} ₽</b>\n"
        f"<b>Вакансии</b>: {'показываю с меткой' if cfg.get('show_vacancies') else 'скрыты (в банке есть)'}\n"
        f"<b>Порог ИИ</b>: <b>{score}</b>/10 — ниже этого карточка в чат не идёт\n"
        f"<b>Админка</b>: <b>{'кнопочная (эта)' if ui == core.UI_MONITOR else 'веб-панель'}</b>\n\n"
        f"<i>Кнопки у поля ввода меняют фильтры — панель обновится сразу. "
        f"«{BACK_LABEL}» вернёт главное меню.</i>"
    )
    return text, FILTERS_KB


def render_archive(conn) -> tuple[str, None]:
    """Архив: сколько неактивных заказов по площадкам + последние причины."""
    rows = conn.execute(
        "SELECT platform, kind, COUNT(*) FROM orders WHERE status='archived' GROUP BY platform, kind ORDER BY 3 DESC"
    ).fetchall()
    if not rows:
        return "📦 <i>Архив пуст — пока нечего убирать.</i>", None
    lines = ["<b>📦 В архиве</b> (неактивные):"]
    mark = {"order": "🚨", "vacancy": "💼"}
    lines += [f"• <b>{h(platform)}</b> {mark.get(kind, '')}: {count}" for platform, kind, count in rows]
    last = conn.execute(
        "SELECT title, archive_reason FROM orders WHERE status='archived' ORDER BY archived_at DESC LIMIT 3"
    ).fetchall()
    if last:
        lines.append("\n<i>Последние:</i>")
        lines += [f"— {h(cut(title or '', 60))}: {h(reason or '—')}" for title, reason in last]
    return "\n".join(lines), None


SECTIONS: dict[str, dict] = {
    "📋 Статус": {"render": render_status, "hint": "здоровье источников и счётчик банка"},
    "🆕 Заказы": {"render": render_recent, "hint": "10 свежих активных из банка"},
    "📊 Статистика": {"render": render_statistics, "hint": "сводка банка и живость источников"},
    "📄 Источники": {"render": render_sources, "hint": "вкл/выкл источников кнопками"},
    "🔍 Фильтры": {"render": render_filters, "hint": "слова, стоп-слова, мин. цена"},
    "📦 Архив": {"render": render_archive, "hint": "что ушло в архив и почему"},
    "🤖 Промт": {"render": None, "hint": "текст задания для модели: посмотреть и изменить"},
}
# Новый раздел: def render_mine(conn) + строка в SECTIONS +
# кнопка с тем же текстом в MAIN_KB выше.

def render_prompt(conn) -> tuple[str, list]:
    """Показать текущий промт для модели и дать две кнопки: изменить и вернуть стандарт.

    Почему в боте: промт — такая же настройка, как порог или слова, а телефон
    под рукой чаще, чем ноутбук. Правка пишется в тот же configs/monitor.json.
    """
    del conn
    cfg = load_config(CONFIG)
    custom = str(cfg.get("llm_prompt") or "").strip()
    body = custom or llm_filter.PROMPT_DEFAULT
    who = "свой" if custom else "встроенный (по умолчанию)"
    return (
        f"<b>🤖 Промт для ИИ</b>\n\n"
        f"Сейчас: <b>{who}</b> · {len(body)} символов\n\n"
        f"<pre>{h(body[:1200])}</pre>\n"
        f"<i>Модель получает этот текст, а в конец подставляется заголовок и описание "
        f"заказа (место подстановки — {{text}}). Ответ она обязана вернуть JSON.</i>",
        PROMPT_ROWS)


def _sources_rows(cfg: dict) -> list:
    """Кнопки источников: каждая со своим состоянием (🟢 вкл / ⚪️ выкл)."""
    rows = []
    for index, source in enumerate(cfg.get("sources", [])):
        mark = "🟢" if source.get("enabled", True) else "⚪️"
        rows.append([(f"{mark} {source.get('name', '?')}", f"a:src:{index}")])
    rows.append([("⬅️ В меню", "s:home")])
    return rows


def show_inline(token: str, chat: int, text: str, rows: list | None = None,
                message_id: int | None = None) -> int | None:
    """Показать панель сообщением с инлайн-кнопками. Есть message_id — правим то же сообщение.

    Почему инлайн, а не reply-клавиатура: владелец 15.11.2026 сказал, что панель
    должна be в чате, а не у поля ввода. Правка того же сообщения вместо новых
    — чтобы лента не забивалась.
    """
    markup = json.dumps(
        {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row]
                             for row in (rows or INLINE_MAIN)]}, ensure_ascii=False)
    body = cut(text)
    if message_id:
        try:
            api(token, "editMessageText", chat_id=chat, message_id=message_id,
                text=body, parse_mode="HTML", reply_markup=markup)
            return message_id
        except Exception:
            pass                      # старше 48 ч или текст не изменился — шлём новое
    result = api(token, "sendMessage", chat_id=chat, text=body, parse_mode="HTML",
                 reply_markup=markup)
    message_id = (result.get("result") or {}).get("message_id")
    if message_id:
        # Панель одна: прежние сообщения-панели убираем, иначе лента забивается.
        for old in PANEL_ID.get(chat, []):
            if old != message_id:
                wipe_later(token, chat, old)
        PANEL_ID[chat] = [message_id]
        save_panel()
    return message_id


def handle_callback(token: str, chat: int, data: str, message_id: int,
                    callback_id: str | None = None) -> None:
    """Нажатие инлайн-кнопки: одно действие и правка того же сообщения."""
    if callback_id:
        try:
            api(token, "answerCallbackQuery", callback_query_id=callback_id)
        except Exception:
            pass
    if data == "a:run":
        start_cycle(token, chat)
        return
    conn = st.connect()
    try:
        cfg = load_config(CONFIG)
        if data == "s:home":
            text, rows = render_status(conn)[0], INLINE_MAIN
        elif data.startswith("s:"):
            label = SECTION_BY_CODE.get(data[2:])
            if not label:
                return
            text = render_section_text(conn, label)
            rows = SECTION_ROWS(label, cfg)
        elif data == "a:price-up":
            core.step_price(1000, CONFIG)
            text, rows = render_filters(conn)[0], SECTION_ROWS("🔍 Фильтры", cfg)
        elif data == "a:price-down":
            core.step_price(-1000, CONFIG)
            text, rows = render_filters(conn)[0], SECTION_ROWS("🔍 Фильтры", cfg)
        elif data == "a:vac":
            core.toggle_vacancies(CONFIG)
            text, rows = render_filters(conn)[0], SECTION_ROWS("🔍 Фильтры", cfg)
        elif data in ("a:s6", "a:s7", "a:s8"):
            core.set_min_score(int(data[-1]), CONFIG)
            text, rows = render_filters(conn)[0], SECTION_ROWS("🔍 Фильтры", cfg)
        elif data == "a:panel":
            # Кнопка «Панель» была в старых сообщениях. Переключать больше нечего:
            # вид один. Просто снимаем зависшую клавиатуру и перерисовываем.
            drop_reply_keyboard(token, chat)
            text, rows = render_filters(conn)[0], SECTION_ROWS("🔍 Фильтры", cfg)
        elif data == "a:cancel":
            drop_prompt(token, chat)
            text, rows = render_status(conn)[0], INLINE_MAIN
        elif data == "a:ui":
            new_ui = core.switch_ui(path=CONFIG)
            hint = "кнопочная" if new_ui == core.UI_MONITOR else "веб-панель (python3 admin_web.py)"
            text = (f"<b>🖥 Админка переключена</b>\n\nСейчас: <b>{hint}</b>\n\n"
                    "<i>Оба вида читают один конфиг и одну базу.</i>")
            rows = INLINE_MAIN
        elif data.startswith("a:src:"):
            index = int(data.rsplit(":", 1)[1])
            sources = cfg.get("sources", [])
            if 0 <= index < len(sources):
                core.toggle_source(sources[index]["name"], CONFIG)
            text = render_section_text(conn, "📄 Источники")
            rows = _sources_rows(load_config(CONFIG))
        elif data in ("a:word-add", "a:word-del"):
            # PENDING заводим ДО prompt(): prompt() сам допишет id сообщения-вопроса.
            PENDING[chat] = {"mode": "add" if data.endswith("add") else "del", "prompt_id": None}
            prompt(token, chat, "Новое слово? Напиши его сообщением." if data.endswith("add")
                   else "Какое слово убрать? Напиши его сообщением.")
            return
        elif data == "a:pr-edit":
            PENDING[chat] = {"mode": "prompt", "prompt_id": None}
            prompt(token, chat, "<b>✏️ Новый промт</b>\n\nПришли одним сообщением. "
                                "Чтобы заказ подставлялся — оставь в тексте {text}.")
            return
        elif data == "a:pr-reset":
            cfg = load_config(CONFIG)
            cfg["llm_prompt"] = ""
            save_config(cfg)
            text = "<b>↩️ Промт сброшен на встроенный</b>\n\n" + render_prompt(conn)[0]
            rows = PROMPT_ROWS
        else:
            return
    finally:
        conn.close()
    show_inline(token, chat, text, rows, message_id)


def render_section_text(conn, label: str) -> str:
    """Текст раздела. Отдельно от кнопок: кнопки строятся по свежему конфигу."""
    entry = SECTIONS[label]
    if entry.get("render") is None:
        return render_prompt(conn)[0]
    return entry["render"](conn)[0]


def SECTION_ROWS(label: str, cfg: dict) -> list:
    """Кнопки раздела: у каждого свои, но всегда есть «В меню»."""
    if label == "🔍 Фильтры":
        return [[("➕ Слово", "a:word-add"), ("➖ Слово", "a:word-del")],
                [("−1000 ₽", "a:price-down"), ("+1000 ₽", "a:price-up")],
                [("💼 Вакансии", "a:vac")],
                [("🤖 6", "a:s6"), ("🤖 7", "a:s7"), ("🤖 8", "a:s8")],
                [("🖥 Сменить админку", "a:ui")],
                [("⬅️ В меню", "s:home")]]
    if label == "📄 Источники":
        return _sources_rows(cfg)
    if label == "🤖 Промт":
        return PROMPT_ROWS
    return INLINE_BACK


# ============================================================
# ЧАСТЬ 2б. Быстрый отклик: не ждём сеть и не рисуем лишнего.
#
# Жалоба владельца 18.09.2026: «долго работает, несколько нажатий отрабатывают
# долго и последовательно». Причина видна в логе: на КАЖДОЕ нажатие бот делал
# вызовы Telegram ПО ОЧЕРЕДИ (прислать панель, удалить прежние, удалить нажатие),
# а «прежних» в panel.json накопилось 28 id — список не чистился, потому что
# удачные удаления тоже оставались в нём. 28 удалений через прокси = 2.4–3.7 с на
# одно нажатие, и с каждой кнопкой становилось хуже. Лечение:
#   1) wipe() различает «удалять больше нечего» и «сорвалось по сети» — мёртвые id
#      не висят в panel.json (см. GONE_MARKS);
#   2) удаление нажатия уходит в фон (wipe_later) — воркер его не ждёт;
#   3) подряд идущие НАВИГАЦИОННЫЕ нажатия схлопываются в одно (coalesce):
#      промежуточные панели всё равно удаляются, конечное состояние то же.
# ============================================================

NAV_LABELS = frozenset(SECTIONS) | {BACK_LABEL}   # перерисовки: разделы меню и «В меню»

_DELETER = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="wipe")


def wipe_later(token: str, chat: int, message_id: int) -> None:
    """Убрать нажатие (или подсказку) В ФОНЕ — воркер не ждёт сеть, отклик мгновенный."""
    try:
        _DELETER.submit(wipe, token, chat, message_id)
    except RuntimeError:                 # пул уже закрыт (выход из процесса) — убираем синхронно
        wipe(token, chat, message_id)


def idle_deleter(timeout: float = 10.0) -> None:
    """Дождаться фоновых удалений (нужно тестам и аккуратному выходу из процесса)."""
    try:
        _DELETER.submit(lambda: None).result(timeout=timeout)
    except Exception:
        pass


def is_nav_press(upd: dict, owner: int) -> bool:
    """True — нажатие только перерисовывает панель (раздел меню или «В меню»).

    Кнопки, где важно КАЖДОЕ нажатие (шаг цены, тумблеры, ввод слова, «Проверить
    сейчас»), сюда не попадают: их схлопывать нельзя.
    """
    message = upd.get("message") or {}
    if message.get("chat", {}).get("id") != owner:
        return False
    return (message.get("text") or "").strip() in NAV_LABELS


def coalesce(batch: list[dict], owner: int) -> tuple[list[dict], list[int]]:
    """Схлопнуть подряд идущие навигационные нажатия → (что играть, что удалить).

    В цепочке подряд идущих навигационных нажатий играем только ПОСЛЕДНЕЕ, а id
    пропущенных сообщений-нажатий отдаём на удаление. Остальные апдейты (текст,
    цена, тумблеры, «Проверить сейчас») играются все и в исходном порядке.
    """
    play: list[dict] = []
    drop: list[int] = []
    run: list[dict] = []            # накопленная цепочка навигационных нажатий

    def flush() -> None:
        if run:
            play.append(run[-1])                                  # играем последнее
            drop.extend(u["message"]["message_id"] for u in run[:-1])
            run.clear()

    for upd in batch:
        if is_nav_press(upd, owner):
            run.append(upd)                                       # копим цепочку
            continue
        flush()
        play.append(upd)
    flush()
    return play, drop


def collect_batch(updates_q: "queue.Queue", first: dict) -> list[dict]:
    """Забрать из очереди первое нажатие И всё, что успело накопиться за ним.

    Пачка нужна, чтобы схлопнуть подряд идущие навигационные нажатия: владелец
    часто жмёт кнопки очередью, и играть пять перерисовок по 3 с смысла нет.
    """
    batch = [first]
    while True:
        try:
            batch.append(updates_q.get_nowait())
        except queue.Empty:
            return batch


def play_batch(token: str, owner: int, batch: list[dict]) -> int:
    """Проиграть пачку нажатий: схлопнуть навигацию, убрать пропущенное, выполнить.

    Возвращает число РЕАЛЬНО выполненных нажатий (для тестов и лога). Пропущенные
    навигационные нажатия не пропадают даром — их сообщения уходят на удаление,
    чтобы лента не засорялась. Долгое нажатие попадает в лог: по этой строке
    («медленная обработка: N с») видно, что именно тормозит.
    """
    updates, superseded = coalesce(batch, owner)
    if len(batch) > len(updates):
        log(f"схлопнул нажатия: {len(batch)} → {len(updates)}")
    for mid in superseded:                  # пропущенные нажатия убираем в фоне
        wipe_later(token, owner, mid)
    for upd in updates:
        started = time.time()
        try:
            route(token, owner, upd)
        except Exception as error:
            log(f"обработка обновления: {error}")
        spent = time.time() - started
        if spent > 1.5:                     # диагностика тормозов: видно, что именно медленное
            log(f"медленная обработка: {spent:.1f} с")
    return len(updates)


# ============================================================
# ЧАСТЬ 3. Действия: разделы рисует show_section(); нажатия разбирают
# handle_reply() + section_action() (ЧАСТЬ 3в). Inline-кнопок в чате владельца
# нет — всё, что можно нажать, reply-кнопка у поля ввода.
# ============================================================

def show_section(token: str, chat: int, section: str, message_id: int | None = None) -> None:
    """Отрисовать раздел инлайн-панелью: текст раздела + его кнопки."""
    if section not in SECTIONS:
        show_inline(token, chat, f"Раздел «{section}» выключен (нет строки в SECTIONS).")
        return
    conn = st.connect()
    try:
        text = render_section_text(conn, section)
    finally:
        conn.close()
    clear_keyboard_once(token, chat)
    show_inline(token, chat, text, SECTION_ROWS(section, load_config(CONFIG)), message_id)


def prompt(token: str, chat: int, text: str) -> None:
    """Спросить текстом: под вопросом инлайн-кнопка «Отмена», клавиатуры нет.

    Клавиатуру не показываем принципиально: панель в чате одна. Отмена приходит
    тем же путём, что и остальные нажатия — callback-ом.
    """
    markup = json.dumps({"inline_keyboard": [[{"text": CANCEL_LABEL, "callback_data": "a:cancel"}]]},
                        ensure_ascii=False)
    res = api(token, "sendMessage", chat_id=chat, text=text,
              parse_mode="HTML", reply_markup=markup)
    PENDING[chat]["prompt_id"] = res["result"]["message_id"]



def drop_prompt(token: str, chat: int) -> None:
    """Убрать подсказку-вопрос, вернуть обычную клавиатуру панели."""
    data = PENDING.pop(chat, None)
    if data and data.get("prompt_id"):
        wipe_later(token, chat, data["prompt_id"])      # подсказка уходит в фоне, панель не ждёт


# ============================================================
# ЧАСТЬ 3б. Живое действие: «Проверить сейчас» — прогнать цикл монитора.
# ============================================================

CYCLE_LOCK = threading.Lock()   # один цикл за раз: второе нажатие не запускает параллельный


def start_cycle(token: str, chat: int) -> None:
    """Запустить цикл в фоне (поток), не замораживая бота.

    Если цикл уже идёт — честно говорим об этом вместо второго запуска.
    """
    if not CYCLE_LOCK.acquire(blocking=False):
        show_text_screen(token, chat, "⏳ Цикл уже идёт — подожди, я пришлю результат.")
        return
    threading.Thread(target=run_cycle, args=(token, chat, True), daemon=True).start()


def run_cycle(token: str, chat: int, release_lock: bool = False) -> None:
    """Запустить monitor.py как отдельный процесс и показать хвост его вывода.

    Отдельный процесс — чтобы долгий цикл не блокировал ответы бота.
    """
    try:
        run_cycle_body(token, chat)
    finally:
        if release_lock:
            CYCLE_LOCK.release()


def run_cycle_body(token: str, chat: int) -> None:
    show_text_screen(token, chat, "⏳ Проверяю источники, это ~20 секунд…")
    try:
        res = subprocess.run(
            [sys.executable, "monitor.py"],
            cwd=APP, capture_output=True, text=True, timeout=300,
        )
        tail = (res.stdout or "").strip().splitlines()[-4:]
        body = "\n".join(tail) if tail else (res.stderr or "пусто")[:500]
        fresh = [line for line in (res.stdout or "").splitlines() if "ПО ФИЛЬТР ПРОШЛО" in line]
        head = f"✅ Цикл выполнен. {fresh[0].split(':')[-1].strip() if fresh else 'новых нет'}"
        show_text_screen(token, chat, f"{head}\n\n{body}")
    except subprocess.TimeoutExpired:
        show_text_screen(token, chat, "⚠️ Цикл не уложился в 5 минут — смотри volume/monitor.log")
    except Exception as error:
        show_text_screen(token, chat, f"⚠️ Не смог запустить цикл: {error}")


# ============================================================
# ЧАСТЬ 3в. Текстовые сообщения от владельца (только то, что нельзя кнопками).
# ============================================================

def handle_reply(token: str, chat: int, text: str, message_id: int) -> bool:
    """Нажата reply-кнопка: меню, кнопка раздела или отмена. True — это кнопка.

    Порядок важен (исследование): сначала показать панель, потом убрать нажатие —
    отклик ощущается мгновенно, а не «нажал и ждал чистку». Причём нажатие убирает
    wipe_later() В ФОНЕ: воркер не ждёт сеть, поэтому десять быстрых нажатий не
    выстраиваются в очередь по 3 секунды. Кнопки разделов проверяются ДО режима
    ввода слова: иначе подпись кнопки уехала бы в фильтры словом (тот же урок, что
    в заготовке tg-admin).
    """
    if text in SECTIONS:                        # главное меню
        show_section(token, chat, text)
        wipe_later(token, chat, message_id)
        return True
    if text == CHECK_LABEL:                     # «Проверить сейчас»
        start_cycle(token, chat)
        wipe_later(token, chat, message_id)
        return True
    if text == CANCEL_LABEL:                    # отмена ввода слова
        drop_prompt(token, chat)
        show_section(token, chat, SEC_FILTERS)
        wipe_later(token, chat, message_id)
        return True
    if section_action(token, chat, text):       # кнопки внутри разделов
        wipe_later(token, chat, message_id)
        return True
    return False


def section_action(token: str, chat: int, text: str) -> bool:
    """Действия кнопок ВНУТРИ разделов. True — нажатие наше, панель перерисована.

    Своя кнопка в разделе → своя ветка здесь. Подписи берём из FILTERS_KB,
    build_sources_kb() и констант выше — тогда клавиатура и маршрут не разъедутся.
    """
    cfg = load_config(CONFIG)

    if text in (WORD_ADD, WORD_DEL):            # слово фильтра: жду ввод текстом
        add = text == WORD_ADD
        PENDING[chat] = {"mode": "add" if add else "del", "prompt_id": 0}
        if add:
            hint = WORD_ADD + " Пришли слово для добавления (например: парсер)"
        else:
            hint = WORD_DEL + " Пришли слово из списка:\n" + ", ".join(cfg.get("keywords", []))
        prompt(token, chat, hint)
        return True

    if text in (PRICE_UP, PRICE_DOWN):          # мин. цена шагом 1000 руб
        step = 1000 if text == PRICE_UP else -1000
        cfg["min_price"] = max(0, int(cfg.get("min_price", 0)) + step)
        save_config(cfg)
        show_section(token, chat, SEC_FILTERS)
        return True

    if text == VAC_LABEL:                       # тумблер «показывать вакансии»
        cfg["show_vacancies"] = not cfg.get("show_vacancies", False)
        save_config(cfg)
        show_section(token, chat, SEC_FILTERS)
        return True

    if text in SCORE_LABELS:                    # порог ИИ: 6 / 7 / 8
        # Порог пишем через ядро: ту же функцию зовёт веб-панель, поэтому два вида
        # админки не могут показать разные числа.
        core.set_min_score(SCORE_LABELS[text], path=CONFIG)
        show_section(token, chat, SEC_FILTERS)
        return True

    if text == UI_LABEL:                        # быстрое переключение вида админки
        new_ui = core.switch_ui(path=CONFIG)
        hint = ("кнопочная (эта)" if new_ui == core.UI_MONITOR
                else "веб-панель — запусти <code>python3 admin_web.py</code> "
                     "и открой http://127.0.0.1:8765")
        show_inline(token, chat,
                    f"<b>🖥 Админка переключена</b>\n\nСейчас: <b>{hint}</b>\n\n"
                    "<i>Оба вида читают один конфиг и одну базу, миграций нет. "
                    "Вернуться можно этой же кнопкой или командой /ui.</i>",
                    SECTION_ROWS("🔍 Фильтры", cfg))
        return True

    if text.startswith(f"{SRC_ON} {SRC_PREFIX}") or text.startswith(f"{SRC_OFF} {SRC_PREFIX}"):
        name = text.split(SRC_PREFIX, 1)[1].strip()      # вкл/выкл источника
        for src in cfg["sources"]:
            if src["name"] == name:
                src["enabled"] = not src.get("enabled", True)
                save_config(cfg)
                break
        show_section(token, chat, SEC_SOURCES)
        return True

    if text == PROMPT_EDIT:                     # ✏️ Изменить промт (reply-режим)
        PENDING[chat] = {"mode": "prompt", "prompt_id": None}
        prompt(token, chat, "<b>✏️ Новый промт</b>\n\nПришли одним сообщением. "
                            "Чтобы заказ подставлялся — оставь в тексте {text}.")
        return True

    if text == PROMPT_RESET:                    # ↩️ Вернуть встроенный промт
        cfg = load_config(CONFIG)
        cfg["llm_prompt"] = ""
        save_config(cfg)
        show_section(token, chat, "🤖 Промт")
        return True

    if text == BACK_LABEL:                      # «В меню»
        drop_prompt(token, chat)
        show_home(token, chat)
        return True

    return False



def handle_text(token: str, chat: int, text: str, message_id: int) -> None:
    """Текст от владельца: либо ответ на «пришли слово», либо резервная команда."""
    wipe(token, chat, message_id)      # своё сообщение владельца убираем сразу: ответ ждём ниже
    pending = PENDING.get(chat)
    if pending and pending.get("mode") == "prompt" and not text.startswith("/"):
        cfg = load_config(CONFIG)
        cfg["llm_prompt"] = text.strip()
        save_config(cfg)
        drop_prompt(token, chat)
        show_section(token, chat, "🤖 Промт")
        return
    if pending and not text.startswith("/"):   # команду в режиме «пришли слово» не считаем словом
        word = text.strip().lower()
        if not word:
            return
        cfg = load_config(CONFIG)
        keywords = [w for w in cfg.get("keywords", [])]
        if pending["mode"] == "add":
            if word not in keywords:
                keywords.append(word)
        else:
            keywords = [w for w in keywords if w != word]
        cfg["keywords"] = keywords
        save_config(cfg)
        drop_prompt(token, chat)          # убрать сообщение-вопрос
        show_section(token, chat, "🔍 Фильтры")
        return
    if text.startswith("/"):
        command = text.split()[0].lstrip("/").split("@")[0]
        targets = {
            "start": "📋 Статус", "status": "📋 Статус", "orders": "🆕 Заказы",
            "stats": "📊 Статистика", "sources": "📄 Источники",
            "filters": "🔍 Фильтры", "archive": "📦 Архив", "prompt": "🤖 Промт",
        }
        if command == "panel":
            # Переключать нечего: панель одна. Команда осталась как «починить» —
            # снимает зависшую клавиатуру и перерисовывает панель.
            KEYBOARD_CLEARED.discard(chat)
            show_home(token, chat)
        elif command == "ui":
            # Переключение вида админки текстом: то же действие, что кнопка «Сменить админку».
            new_ui = core.switch_ui(path=CONFIG)
            show_text_screen(token, chat,
                             f"🖥 Админка: <b>{'кнопочная' if new_ui == core.UI_MONITOR else 'веб-панель'}</b>")
        elif command == "check":
            start_cycle(token, chat)
        elif command in targets:
            show_section(token, chat, targets[command])
        return
    show_text_screen(token, chat, "Я понимаю кнопки ниже 👇")


# ============================================================
# ЧАСТЬ 4. Главный цикл: long polling (не трогать при адаптации).
# Принимаем только владельца: чужой chat_id отбрасывается и пишется в лог.
# ============================================================

def route(token: str, owner: int, upd: dict) -> None:
    """Развести одно обновление Telegram: нажатие кнопки или текст."""
    if "callback_query" in upd:
        cb = upd["callback_query"]
        message = cb.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat != owner:
            try:
                api(token, "answerCallbackQuery", callback_query_id=cb["id"],
                    text="Это чужая панель")
            except Exception:
                pass
            return
        handle_callback(token, chat, cb.get("data") or "", message.get("message_id"), cb.get("id"))
        return

    message = upd.get("message") or {}
    chat = message.get("chat", {}).get("id")
    if chat is None:
        return
    if chat != owner:
        log(f"чужое сообщение от {chat} — игнор")
        return
    text = (message.get("text") or "").strip()
    # Сообщение владельца в чате не нужно: убираем сразу, ответ придёт панелью.
    wipe_later(token, chat, message["message_id"])
    if not text:
        return
    if text == "/check":
        start_cycle(token, chat)
    elif handle_reply(token, chat, text, message["message_id"]):
        return                                  # это была reply-кнопка меню
    elif text.startswith("/") or PENDING.get(chat):
        handle_text(token, chat, text, message["message_id"])


def acquire_single_run() -> int:
    """Защита от двух поллеров (409 Conflict): блокируем файл на всё время работы.
    Вернёт 1, если уже запущен второй экземпляр."""
    lock_path = APP.parent / "volume" / "bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return 1
    handle.write(str(os.getpid()))
    handle.flush()
    global _LOCK_HANDLE
    _LOCK_HANDLE = handle             # держим объект живым до конца процесса
    return 0


def skip_pending(token: str) -> int:
    """Подтвердить накопившиеся за простой апдейты БЕЗ обработки (старые кнопки не жмутся)."""
    try:
        res = api(token, "getUpdates", timeout=0, limit=100,
                  allowed_updates=json.dumps(["message", "callback_query"]))
        items = res.get("result", [])
        if items:
            last = items[-1]["update_id"] + 1
            api(token, "getUpdates", offset=last, timeout=0,
                allowed_updates=json.dumps(["message", "callback_query"]))
            return len(items)
    except Exception as error:
        log(f"скип старых апдейтов: {error}")
    return 0


def main() -> int:
    env = load_env(APP / ".env")
    token = env.get("BOT_TOKEN", "").strip()
    owner_raw = env.get("CHAT_ID", "").strip()
    if not token or not owner_raw:
        print("нет BOT_TOKEN или CHAT_ID в app/.env — см. docs/RUN.md")
        return 1
    owner = int(owner_raw) if owner_raw.lstrip("-").isdigit() else 0

    if acquire_single_run():
        log("бот уже запущен — второй экземпляр не поднимаю (защита от 409)")
        return 0
    load_panel()
    dropped = skip_pending(token)
    if dropped:
        log(f"пропустил {dropped} накопившихся апдейтов (нажатия во время простоя)")
    log(f"админка запущена, владелец: {owner}")
    show_home(token, owner)

    offset = 0
    updates_q: queue.Queue = queue.Queue()
    poll_state = {"last_ok": time.time(), "conn": None}

    def poller() -> None:
        """Только качает апдейты в очередь — обработка его не задерживает.

        Два правила скорости:
        1) timeout=15 (long-poll) при сокете 25 с: за прокси/VPN долгое удержание
           соединения рвётся («read operation timed out»), а короткий цикл —
           устойчив; лишних запросов почти нет, ответ приходит мгновенно.
        2) обработку не задерживает: поллер только кладёт апдейт в очередь, панель
           рисует воркер. Inline-кнопок в чате владельца нет (урок У57), поэтому
           снимать «крутилку» тоже не с чего.

        Живучесть (18.09.2026): раз в 5 минут пишем «поллер жив» — раньше молчание
        лога выглядело как «бот ничего не делает». Сторож ниже рвёт зависшее
        соединение, если ответа нет дольше 90 с.
        """
        nonlocal offset
        fails = 0
        beat = time.time()
        for_beat = 0
        while True:
            try:
                poll_state["conn"] = _telegram_conn(25)
                res = api(token, "getUpdates", offset=offset, timeout=15, _sock=25,
                          allowed_updates=json.dumps(["message", "callback_query"]))
                fails = 0
                poll_state["last_ok"] = time.time()
                got = res.get("result", [])
                for_beat += len(got)
                for upd in got:
                    offset = upd["update_id"] + 1
                    updates_q.put(upd)
                if time.time() - beat > 300:
                    log(f"поллер жив: апдейтов за 5 мин {for_beat}")
                    beat, for_beat = time.time(), 0
            except urllib.error.HTTPError as error:
                if error.code == 409:
                    log("409: второй поллер дёргает токен — жду 15 с")
                    time.sleep(15)
                else:
                    log(f"HTTP {error.code} при опросе — пауза 5 с")
                    time.sleep(5)
            except Exception as error:
                # сетевой обрыв: сразу пробуем снова (0.5 с), но не в «горячий» цикл
                fails += 1
                log(f"опрос Telegram: {error} (сбой №{fails})")
                time.sleep(0.5 if fails < 5 else 3)

    def poller_watchdog() -> None:
        """Сторож опроса: нет успешного ответа дольше 90 с → рвём соединение поллера.

        Зачем: keep-alive сокет через прокси иногда зависает так, что чтение не
        возвращается вовсе (не спасает и таймаут). Тогда бот молчит: кнопки в
        Telegram «нажимаются», а реакции нет. Разрыв соединения из другого потока
        заставляет читающий поток получить исключение и переподключиться.
        """
        while True:
            time.sleep(20)
            silent = time.time() - poll_state["last_ok"]
            if silent > 90:
                log(f"поллер молчит {int(silent)} с — переподключаюсь")
                poll_state["last_ok"] = time.time()   # не спамим лог каждые 20 с
                conn = poll_state.get("conn")
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=poller_watchdog, daemon=True).start()

    # Один worker, строго по очереди: нажатия обрабатываются по одному,
    # панели не гоняются наперегонки. Но перед обработкой забираем ВСЮ уже
    # накопившуюся очередь и схлопываем подряд идущие навигационные нажатия
    # (пять раз «🔍 Фильтры» = одна перерисовка, а не пять по 3 с — жалоба
    # владельца 18.09.2026 «несколько нажатий долго и последовательно»).
    while True:
        upd = updates_q.get()
        play_batch(token, owner, collect_batch(updates_q, upd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
