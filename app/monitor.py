#!/usr/bin/env python3
"""monitor.py — один цикл: скачать → отфильтровать словами → оценить ИИ → доставить.

Запуск:  python3 monitor.py            # рабочий цикл (пишет банк, шлёт в Telegram)
         python3 monitor.py --dry-run  # показать находки, ничего не сохраняя
Только стандартная библиотека Python 3.9+ (проверено на 3.9.6 и 3.14).

Порядок шагов и почему именно такой:
  1) источники → orders.db. Заказ попадает в банк даже если не прошёл фильтр:
     «отсеяли» и «не видели» — разные вещи, и второе нельзя восстановить.
  2) фильтр слов — дешёвый отсев ДО модели. Модель стоит времени, слова бесплатны.
  3) ИИ (llm_filter) — оценка 0–10 и короткая суть. Результат в llm_results.
  4) доставка — только score >= llm_min_score. Каждая отправка проверяется,
     и только после успеха заказ помечается доставленным (P0 из плана: раньше
     упавшая отправка теряла заказ навсегда).
  5) CSV и, если задан адрес, Google Таблица через вебхук Apps Script.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import llm_filter
import net
import sheet_webhook
import storage as st
from sources import (StructureChanged, fetch, load_env, parse_freelanceru, parse_kadrof,
                     parse_kwork, parse_rss)

ROOT = Path(__file__).resolve().parent.parent          # .../ai-lead-radar
APP = Path(__file__).resolve().parent                  # .../ai-lead-radar/app
MAX_TG_MESSAGE = 3800   # запас до лимита Telegram 4096
MAX_ITEMS_PER_RUN = 20  # сколько карточек собираем за цикл (источник при этом не обрываем)
LLM_STOP_AFTER = 3      # стоп-кран: столько отказов модели подряд — и цикл перестаёт её звать
                        # (план §5.2: «три отказа подряд — остановить прогон и доложить»).
                        # Смысл: при 401/429/обрыве сети каждый следующий вызов всё равно
                        # упадёт, но съест минуту ожидания. Останавливаемся и докладываем.

PRICE_RE = re.compile(r"(\d[\d\s]{2,11})\s*(?:₽|руб|р\b)", re.IGNORECASE)


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


HEARTBEAT = ROOT / "volume" / "heartbeat.json"
# Последнее НЕнулевое число позиций по источнику: отличает «вёрстка поехала» от
# «заказов правда нет». Появилось 20.09.2026 после разбора этой дыры.
YIELD = ROOT / "volume" / "source-yield.json"


def heartbeat(status: str, sent: int = 0, failed: int = 0) -> None:
    """Внешний признак «механизм жив» — файл со временем последнего цикла.

    Строку в логе печатает сам механизм, поэтому она не доказательство: цикл может
    умереть, а последняя строка останется. Файл обновляется каждый цикл — это
    независимая проверка, её можно вписать в гейт.
    """
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(
            json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "status": status, "sent": sent, "failed": failed},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as error:                       # не роняем цикл из-за признака жизни
        log(f"не записал признак жизни: {error}")


def heartbeat_status(dry: bool, failed: int) -> str | None:
    """Какой статус писать в признак жизни. `None` — не писать вовсе.

    Холостой прогон обещает «ничего не сохранять», а гейт живости судит по времени
    файла — иначе холостой прогон выдал бы мёртвый цикл за живой (аудит 20.09.2026).
    """
    if dry:
        return None
    return "partial" if failed else "ok"


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(cfg.get("sources"), list), "в конфиге нет sources"
    return cfg


def age_text(published) -> str:
    if published is None:
        return "?"
    minutes = max(0, int((datetime.now(timezone.utc) - published).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} мин"
    if minutes < 1440:
        return f"{minutes // 60} ч"
    return f"{minutes // 1440} дн"


def too_old(item: dict, max_age_days: int) -> bool:
    """Карточка старше max_age_days суток? Дата неизвестна — считаем свежей.

    Зачем (слово владельца 18.09.2026): «живой» источник может отдавать архив —
    канал, мёртвый с 2023 года, показывал трёхлетние заказы как свежие. Источник
    может переопределить правило своим полем max_age_days.
    """
    if not max_age_days:
        return False
    published = item.get("published")
    if published is None:
        return False
    if published.tzinfo is None:        # дата без зоны — считаем UTC, а не падаем
        published = published.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - published).total_seconds() > max_age_days * 86400


def log_stale(source: dict, stats: dict, max_age_days: int) -> None:
    """Сказать в лог, сколько карточек отброшено по свежести (0 — молчим)."""
    if stats.get("stale"):
        log(f"{source['name']}: отброшено по свежести (старше {max_age_days} дн.): {stats['stale']}")


def find_price(text: str) -> int | None:
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    return int(re.sub(r"\s", "", match.group(1)))


def contains(word: str, haystack: str) -> bool:
    """Ключевое слово по НАЧАЛУ (бот ловит бота/боты/ботов, но не «работают»)."""
    return re.search(r"\b" + re.escape(word), haystack) is not None


def stop_cock(failures: int) -> bool:
    """Пора ли прекращать оценку: три отказа модели подряд.

    Вынесено отдельной функцией, чтобы порог проверялся тестом, а не жило
    числом внутри цикла. План §5.2: «три отказа подряд — остановить прогон
    и доложить».
    """
    return int(failures) >= LLM_STOP_AFTER


def contains_stop(word: str, haystack: str) -> bool:
    """Стоп-слово — только ЦЕЛИКОМ или точной фразой, не по началу.

    Префиксы здесь запрещены: иначе стоп-слово «seo» убивало бы любое слово,
    начинающееся на «seo». Граница слова (`\\b`) при этом считает дефис границей,
    поэтому «SEO-продвижение» стоп-словом «seo» всё-таки ловится — это ожидаемо,
    ведь там отдельное слово «SEO». Не ловится только склейка вида «seoshop».
    """
    if " " in word:
        return word in haystack
    return re.search(r"\b" + re.escape(word) + r"\b", haystack) is not None


def passes_filter(item: dict, cfg: dict, source: dict | None = None) -> tuple[bool, str]:
    """Проверка карточки по словам (с переопределениями источника). Вернуть (прошёл, причина)."""
    src = source or {}
    haystack = f"{item['title']} {item['description']}".lower()
    words = [w.lower() for w in (src.get("keywords") or cfg.get("keywords", [])) if str(w).strip()]
    exclude = [w.lower() for w in cfg.get("exclude_words", []) if str(w).strip()]
    exclude += [w.lower() for w in src.get("exclude_words", []) if str(w).strip()]
    if exclude and any(contains_stop(w, haystack) for w in exclude):
        return False, "стоп-слово"
    if st.classify_kind(item.get("title"), item.get("description")) == "vacancy" \
            and not cfg.get("show_vacancies", False):
        return False, "вакансия (тумблер выключен)"
    if words and not any(contains(w, haystack) for w in words):
        return False, "нет ключевых слов"
    min_price = int(src.get("min_price", cfg.get("min_price", 0)) or 0)
    if min_price:
        price = item.get("price") or find_price(haystack)
        if price is not None and price < min_price:
            return False, f"цена {price} < {min_price}"
    return True, "ok"


def tg_send(text: str, env: dict) -> bool:
    token, chat = env.get("BOT_TOKEN", ""), env.get("CHAT_ID", "")
    if not token or not chat:
        return False
    data = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "false"}
    ).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    # api.telegram.org с нашей сети напрямую не отвечает (TLS-рукопожатие висит).
    # Прокси берём из .env: TG_PROXY_URL, а если его нет — LLM_PROXY_URL (тот же туннель).
    opener = net.opener(net.proxy_of(env, "TG_PROXY_URL", "LLM_PROXY_URL"))
    for attempt in (1, 2, 3):
        try:
            with opener.open(req, timeout=20) as resp:
                if resp.status == 429:
                    wait = _tg_retry_after(resp)
                    log(f"Telegram просит подождать ({wait} с, попытка {attempt}/3)")
                    time.sleep(wait)
                    continue
                return resp.status == 200
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 3:
                wait = _tg_retry_after(error)
                log(f"Telegram просит подождать ({wait} с, попытка {attempt}/3)")
                time.sleep(wait)
                continue
            log(f"Telegram не ответил: {error}")
            return False
        except Exception as error:  # сеть/токен — не роняем цикл
            log(f"Telegram не ответил: {error}")
            return False
    return False


def send_chunks(chunks: list[str], env: dict) -> tuple[int, int]:
    """Разослать пачки и вернуть (дошло, не дошло). Результат КАЖДОЙ отправки проверяется.

    Раньше вызывающий код игнорировал возврат и печатал «Отправлено N» всегда — лог
    выглядел зелёным при мёртвом токене и при 429. Вынесено в функцию, чтобы это
    можно было проверить тестом.
    """
    sent = sum(1 for chunk in chunks if tg_send(chunk, env))
    return sent, len(chunks) - sent


def _tg_retry_after(resp) -> int:
    """Секунды из 429-ответа Telegram (тело или заголовок Retry-After)."""
    try:
        body = resp.read() if hasattr(resp, "read") else b""
        params = json.loads(body or b"{}").get("parameters", {})
        return max(1, int(params.get("retry_after", 1)))
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return max(1, int(resp.headers.get("Retry-After", 1)))
    except (ValueError, TypeError, AttributeError):
        return 1


def card(source: str, item: dict, kind: str = "order", llm: dict | None = None) -> str:
    """Карточка заказа для Telegram. С оценкой ИИ, если она есть.

    Без llm — прежний вид (заголовок, цена, описание). С llm — сверху оценка и суть:
    владелец читает одну строку и решает, открывать ли заказ.
    """
    if kind == "order" and st.classify_kind(item.get("title"), item.get("description")) == "vacancy":
        kind = "vacancy"
    published = item["published"].strftime("%d.%m %H:%M") if item.get("published") else ""
    when = item.get("published_text") or published or ""
    price = item.get("price") or find_price(f"{item['title']} {item['description']}")
    head = f"{chr(0x1F4BC) if kind == 'vacancy' else chr(0x1F6A8)} {item['title'].strip()}"
    # Строка «где и когда» собирается из того, что известно. Раньше неизвестное
    # печаталось как «? назад · ?» — мусор в карточке (Kwork не отдаёт дату вовсе).
    meta = [f"📍 {source}"]
    age = age_text(item.get("published"))
    if age != "?":
        meta.append(f"{age} назад")
    if when:
        meta.append(when)
    if item.get("note"):
        meta.append(str(item["note"]))
    if price and not llm:
        meta.append(f"💰 ~{price:,} ₽".replace(",", " "))
    # Пустая строка между смысловыми блоками: без неё две карточки подряд в чате
    # читаются как одна стена текста (слово владельца 15.11.2026: «как будто слиплись»).
    lines = [head, " · ".join(meta), ""]
    if llm:
        parts = [f"🤖 Оценка {llm.get('score')}/10"]
        if llm.get("category"):
            parts.append(str(llm["category"]))
        # Деньги печатаются ОДИН раз: бюджет от модели, а если его нет — цена объявления.
        money = llm.get("budget") or (f"~{price:,} ₽".replace(",", " ") if price else "")
        if money:
            parts.append(f"💰 {money}")
        lines.append(" · ".join(parts))
        if llm.get("summary"):
            lines.append(f"📝 {llm['summary'][:300]}")
        if llm.get("reason"):
            lines.append(f"💡 {llm['reason'][:200]}")
    elif item.get("description"):
        lines.append(f"📝 {item['description'][:400]}")
    if item.get("profile"):
        lines.append(f"👤 {item['profile']}")
    if item.get("url"):
        lines.append("")
        lines.append(f"🔗 {item['url']}")
    return "\n".join(lines)


def fetch_items(source: dict) -> tuple[list[dict], str | None]:
    """Скачать и разобрать источник. Вернуть (карточки, текст ошибки | None)."""
    kind, url = source.get("type"), source.get("url", "")
    try:
        if kind in ("flru_rss", "rss"):
            items = parse_rss(fetch(url))
        elif kind == "kwork_page":
            items = parse_kwork(fetch(url).decode("utf-8", "ignore"))
        elif kind == "freelanceru_page":
            items = parse_freelanceru(fetch(url).decode("utf-8", "ignore"))
        elif kind == "kadrof_page":
            items = parse_kadrof(fetch(url).decode("utf-8", "ignore"))
        else:
            log(f"{source['name']}: неизвестный тип {kind!r} — пропускаю")
            return [], f"неизвестный тип {kind!r}"
    except urllib.error.HTTPError as error:
        if error.code in (403, 429):
            log(f"{source['name']}: сайт просит притормозить (HTTP {error.code}) — пропускаю цикл")
        else:
            log(f"{source['name']}: источник не отвечает (HTTP {error.code})")
        return [], f"HTTP {error.code}"
    except StructureChanged as error:
        log(f"{source['name']}: СТРУКТУРА ИЗМЕНИЛАСЬ — разбор не нашёл данные ({error})")
        return [], f"структура страницы изменилась: {error}"
    except Exception as error:
        log(f"{source['name']}: источник не отвечает ({error})")
        return [], str(error)
    log(f"{source['name']}: скачал {len(items)} позиций")
    return items, None


def yield_alarm(source: dict, count: int, remember: bool = True) -> str | None:
    """Проверить источник по истории: раньше давал позиции, а теперь ноль?

    `remember=False` — ничего не записывать (в `--dry-run` история только читается).
    Зачем: парсеры на смене вёрстки честно возвращают пустой список, и отличить
    «заказов нет» от «разбор сломался» из одной страницы нельзя. А по истории — можно.
    """
    name = source.get("name") or "?"
    try:
        data = json.loads(YIELD.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    was = int(data.get(name) or 0)
    if count:
        if remember and data.get(name) != count:
            data[name] = count
            try:
                YIELD.parent.mkdir(parents=True, exist_ok=True)
                YIELD.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as error:
                log(f"{name}: не смог записать историю выдачи ({error})")
        return None
    if was > 0:
        return f"источник отдал 0 позиций, а в прошлый раз {was} — похоже, вёрстка изменилась"
    return None


def run_source(source: dict, cfg: dict, dry: bool, conn=None) -> tuple[list[dict], dict]:
    """Один источник: обновить банк. Вернуть (новые по фильтру, статистика).

    Два правила, которые здесь держатся тестами:
      * лимит карточек за цикл НЕ обрывает источник: остальные позиции всё равно
        попадают в банк, просто не идут в карточки в этом цикле (P0 из плана);
      * `mark_notified` здесь значит только «прошёл фильтр». Доставку отмечает
        set_delivered() после успешной отправки.
    """
    fresh: list[dict] = []
    stats = {"items": 0, "new": 0, "stale": 0, "error": None, "capped": 0}
    items, error = fetch_items(source)
    if not error:
        error = yield_alarm(source, len(items), remember=not dry)
        if error:
            log(f"{source['name']}: {error}")
    stats["items"], stats["error"] = len(items), error
    if error and not items:
        return fresh, stats
    max_age_days = int(source.get("max_age_days", cfg.get("max_age_days", 0)) or 0)
    if dry:
        for item in items:
            if too_old(item, max_age_days):
                stats["stale"] += 1
                continue                        # архив источника: и в показе не нужен
            if passes_filter(item, cfg, source)[0]:
                fresh.append({**item, "id": f"{source['name']}:{item['id']}"})
                if len(fresh) >= MAX_ITEMS_PER_RUN:
                    break
        log_stale(source, stats, max_age_days)
        return fresh, stats
    platform = source.get("platform") or st.platform_of(source["name"])
    for item in items:
        item_id = f"{source['name']}:{item['id']}"
        if too_old(item, max_age_days):
            stats["stale"] += 1
            continue                            # в банк не пишем и владельцу не шлём
        first_time = st.is_new(conn, item_id)
        st.upsert(conn, platform, source["name"], {**item, "id": item_id})
        if not first_time:
            continue  # уже в банке: last_seen обновили — значит, заказ всё ещё жив в ленте
        stats["new"] += 1
        ok, reason = passes_filter(item, cfg, source)
        st.mark_notified(conn, item_id, ok)
        st.dump_order(platform, {**item, "id": item_id}, ok, reason)
        if not ok:
            continue
        if len(fresh) >= MAX_ITEMS_PER_RUN:
            # Не обрываем источник: заказ уже в банке, в карточки попадёт следующим
            # циклом. Раньше здесь стоял break — и хвост ленты не попадал в банк вовсе.
            stats["capped"] += 1
            continue
        fresh.append({**item, "id": item_id})
    if stats["capped"]:
        log(f"{source['name']}: {stats['capped']} сверх лимита {MAX_ITEMS_PER_RUN} — в банке, карточки позже")
    log_stale(source, stats, max_age_days)
    return fresh, stats


def is_lead(result: dict, item: dict, min_score: int) -> bool:
    """Лид = оценка не ниже порога И есть бюджет.

    Бюджет берём из ответа модели, иначе из объявления. Нет ни там, ни там —
    не лид: такие не идут ни в чат, ни в таблицу (слово владельца 15.11.2026).
    Один список на оба канала — поэтому «в чате то же, что в таблице» держится
    не обещанием, а общей функцией.
    """
    if result.get("status") != llm_filter.STATUS_OK:
        return False
    if int(result.get("score") or 0) < int(min_score):
        return False
    return sheet_webhook.pick_budget(result.get("budget"), item.get("price")) != ""


def deliver(conn, tasks: list[tuple[str, dict, dict | None]], env: dict) -> tuple[int, int]:
    """Доставить карточки. Вернуть (дошло, не дошло).

    tasks — (id, item, llm|None). Отметка delivered ставится ТОЛЬКО после того, как
    Telegram подтвердил отправку. Не подтвердил — заказ остаётся в очереди
    (pending_delivery) и уйдёт следующим циклом.
    """
    sent = failed = 0
    for item_id, item, llm in tasks:
        text = card(item.get("source") or item.get("source_name") or "?", item, llm=llm)
        if tg_send(text, env):
            st.set_delivered(conn, item_id, True)
            sent += 1
        else:
            st.set_delivered(conn, item_id, False)
            failed += 1
    return sent, failed


def check_llm(env: dict) -> int:
    """Проверить модель одним коротким запросом. Код 0 — работает, 1 — нет.

    Зачем отдельная команда: 15.11.2026 бесплатные модели OpenCode исчерпали лимит,
    и цикл тратил по две минуты на каждый вызов, сообщая «таймаут». Один запрос
    с внятной причиной экономит эти минуты и сразу говорит, что менять.
    """
    conf = llm_filter.settings(env)
    mode = conf["provider"]
    log(f"режим: {mode} | модель: {conf['model'] or 'не задана'}")
    if not conf["model"]:
        log("ОТКАЗ: в .env не задан LLM_MODEL — модель звать нечем")
        return 1
    started = time.time()
    text, error, used = llm_filter.ask("Ответь одним словом: пинг", env, conf)
    took = time.time() - started
    if text.strip():
        log(f"ОК ({used}): ответ «{text.strip()[:40]}» за {took:.1f} с")
        return 0
    log(f"ОТКАЗ ({used}) за {took:.1f} с: {error or 'пустой ответ без ошибки'}")
    if mode == "opencode_cli":
        log("Что делать: сменить LLM_MODEL на доступную, обновить клиент "
            "(opencode upgrade --major) или перейти на свой ключ:")
        log("  LLM_PROVIDER=openai_http, LLM_BASE_URL=..., LLM_MODEL=..., LLM_API_KEY=...")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Радар заказов: биржи → слова → ИИ → Telegram/CSV")
    parser.add_argument("--config", default=str(ROOT / "configs" / "monitor.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="показать находки, ничего не сохранять и не слать")
    parser.add_argument("--no-llm", action="store_true",
                        help="не звать модель: показать только отсев по словам")
    parser.add_argument("--check-llm", action="store_true",
                        help="проверить модель одним запросом и сказать, жива ли она")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    env = load_env(str(APP / ".env"))
    if args.check_llm:
        return check_llm(env)
    started = time.time()
    llm_conf = llm_filter.settings(env)
    llm_on = bool(llm_conf["model"]) and not args.no_llm
    min_score = int(cfg.get("llm_min_score", 7))
    if not llm_on:
        log("ИИ выключен (нет LLM_MODEL в .env или --no-llm): работают только слова")

    all_new: list[tuple[str, dict]] = []
    pause = int(cfg.get("pause_seconds", 15))
    conn = None if args.dry_run else st.connect()
    for index, source in enumerate(cfg["sources"]):
        if not source.get("enabled", True):
            continue
        if index and pause:
            time.sleep(pause)  # вежливость: пауза между источниками, не бьём залпом
        fresh, stats = run_source(source, cfg, args.dry_run, conn)
        if conn:
            st.run_log(conn, source["name"], stats)
        for item in fresh:
            all_new.append((source["name"], item))

    if args.dry_run:
        log(f"dry-run: под фильтр слов попало {len(all_new)}")
        for source_name, item in all_new:
            print(card(source_name, item), "\n" + "—" * 60)
        log("dry-run: банк не тронут, в Telegram не слалось")
        return 0

    # -------- шаг ИИ
    leads: list[tuple[str, dict, dict | None]] = []
    if llm_on:
        # Предел вызовов модели за цикл. Заказы в банке от этого не теряются: они уже
        # лежат в orders и вернутся в обработку следующим циклом (first_time=False —
        # значит «уже видели», но оценка у них ещё не стоит, и их надо до-оценить).
        limit_llm = int(cfg.get("max_llm_per_run", 25) or 0)
        queue = all_new[:limit_llm] if limit_llm else all_new
        if limit_llm and len(all_new) > limit_llm:
            log(f"к модели за этот цикл: {limit_llm} из {len(all_new)} — остальные следом")
        # Добор хвоста: заказы, которые прошли слова в прошлых циклах, но до модели
        # не дошли (лимит). Без этого они остались бы без оценки навсегда.
        if limit_llm and len(queue) < limit_llm:
            seen = {item["id"] for _src, item in queue}
            for row in st.unevaluated(conn, limit_llm - len(queue)):
                if row["id"] in seen:
                    continue
                published = None
                if row.get("published"):
                    try:
                        published = datetime.fromisoformat(row["published"])
                    except ValueError:
                        published = None
                queue.append((row["source_name"], {
                    "id": row["id"], "title": row["title"], "description": row["description"] or "",
                    "url": row["url"] or "", "price": row["price"], "published": published,
                }))
            if len(queue) > len(all_new):
                log(f"добор из банка: {len(queue) - len(all_new)} заказов без оценки")
        failures = 0
        for index, (source_name, item) in enumerate(queue):
            result = llm_filter.qualify(item, cfg, env, llm_conf)
            st.save_llm(conn, item["id"], result)
            if result["status"] != llm_filter.STATUS_OK:
                failures += 1
                log(f"ИИ: {item['id']} — {result['status']} ({result['reason'][:80]}), повтор позже")
                if stop_cock(failures):
                    # Дальше звать модель бессмысленно: те же 401/429/обрыв сети.
                    # Уже найденные лиды не теряем — они уйдут в доставку как обычно.
                    left = len(queue) - index - 1
                    log(f"СТОП-КРАН: {failures} отказа модели подряд — прекращаю оценку, "
                        f"без оценки осталось {left}")
                    if env.get("BOT_TOKEN") and env.get("CHAT_ID"):
                        try:
                            tg_send(f"⚠️ Стоп-кран: {failures} отказа модели подряд "
                                    f"({result['status']}). Оценка остановлена, "
                                    f"без оценки осталось {left}.", env)
                        except Exception:
                            pass
                    break
                continue
            failures = 0
            if is_lead(result, item, min_score):
                leads.append((item["id"], dict(item, source=source_name), result))
            elif int(result.get("score") or 0) >= min_score:
                # Порог пройден, но бюджета нет ни в ответе модели, ни в объявлении.
                # Такие не идут ни в чат, ни в таблицу: цена — часть решения.
                log(f"ИИ: {result['score']}/10 · без бюджета — отсеиваю · {item['id']}")
                st.set_delivered(conn, item["id"], True)
            else:
                # Не лид: повторять нечего — помечаем «обработано», чтобы не всплывал
                st.set_delivered(conn, item["id"], True)
            log(f"ИИ: {result['score']}/10 · {result['category'][:30]} · {item['id']}")
    else:
        for source_name, item in all_new:
            leads.append((item["id"], dict(item, source=source_name), None))

    # -------- доставка: новые лиды + недоставленные с прошлых циклов
    known = {task[0] for task in leads}
    for row in st.pending_delivery(conn, min_score, MAX_ITEMS_PER_RUN):
        if row["id"] in known:
            continue
        if sheet_webhook.pick_budget(row.get("budget"), row.get("ad_price")) == "":
            st.set_delivered(conn, row["id"], True)      # без бюджета — не лид
            continue
        # Возраст карточки берём из банка: без этого повторная отправка показывала бы «?»
        # вместо «6 ч назад» — как будто заказ только что появился.
        published = None
        if row.get("when_seen"):
            try:
                published = datetime.fromisoformat(row["when_seen"])
            except ValueError:
                published = None
        item = {"id": row["id"], "title": row["title"], "url": row["url"],
                "description": "", "price": row["price"], "published": published,
                "source": row["source_name"]}
        leads.append((row["id"], item, {"score": row["score"], "category": row["category"],
                                        "budget": row["budget"], "summary": row["summary"]}))

    sent = failed = 0
    if leads:
        log(f"К доставке: {len(leads)}")
        sent, failed = deliver(conn, leads, env)
        if failed:
            log(f"Telegram НЕ принял {failed} из {len(leads)} — остаются в очереди на повтор")
        else:
            log(f"Отправлено в Telegram: {sent}")
    else:
        log("Лидов нет: ни один заказ не дотянул до порога.")

    # -------- витрины: CSV и Google Таблица
    path = st.export_leads_csv(conn)
    rows = max(0, len(path.read_text(encoding="utf-8").splitlines()) - 1)
    log(f"CSV: {path} ({rows} строк)")
    posted, why = sheet_webhook.push_pending(conn, env, min_score)
    if posted:
        log(f"Таблица: отправлено строк {posted}")
    elif why:
        log(f"Таблица: {why}")

    archived = st.archive_pass(conn, cfg)
    if archived:
        log(f"В архив: {len(archived)} — старше недели или пропали из ленты")
    conn.close()

    status = heartbeat_status(args.dry_run, failed)
    if status is None:
        log("dry-run: признак жизни не обновляю — холостой прогон не цикл")
    else:
        heartbeat(status, sent, failed)
    log(f"Цикл закончен за {time.time() - started:.1f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
