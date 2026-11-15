"""sources.py — источники заказов для ai-lead-radar: скачать и разобрать.

Только стандартная библиотека: urllib + xml.etree. Ничего ставить не нужно.
"""
from __future__ import annotations

import email.utils
import html as html_module
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) ai-lead-radar/0.1"
TIMEOUT = 25  # секунд


def fetch(url: str) -> bytes:
    """Скачать URL. Честный User-Agent, таймаут, ошибка — исключение с понятным текстом."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    """Убрать теги, раскодировать сущности HTML (&#8381; → ₽), сжать пробелы."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_pubdate(raw: str) -> datetime | None:
    """RFC-822 дату из RSS в datetime с зоной (None — если не разобрали)."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_rss(data: bytes) -> list[dict]:
    """Разобрать RSS 2.0 / Atom в список карточек заказа.

    Возвращает dict: id, title, url, description, published (datetime|None).
    """
    root = ET.fromstring(data)
    items: list[dict] = []

    def _txt(node, path: str) -> str:
        found = node.find(path)
        return (found.text or "").strip() if found is not None and found.text else ""

    for channel_item in root.iter():  # RSS: <item>, Atom: <entry>
        tag = channel_item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = strip_html(_txt(channel_item, "title") or _txt(channel_item, "{http://www.w3.org/2005/Atom}title"))
        link = _txt(channel_item, "link")
        if not link:  # Atom: <link href="...">
            for a in channel_item:
                if a.tag.rsplit("}", 1)[-1] == "link" and a.get("href"):
                    link = a.get("href")
                    break
        guid = _txt(channel_item, "guid") or link or title
        desc = strip_html(_txt(channel_item, "description") or _txt(channel_item, "{http://www.w3.org/2005/Atom}summary"))
        published = parse_pubdate(_txt(channel_item, "pubDate") or _txt(channel_item, "updated") or _txt(channel_item, "{http://www.w3.org/2005/Atom}updated"))
        if title or link:
            items.append({"id": guid, "title": title, "url": link, "description": desc, "published": published})
    return items


def load_env(path: str) -> dict[str, str]:
    """Прочитать .env вида KEY=VALUE (комментарии # игнорируем)."""
    env: dict[str, str] = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return env


# ---------------- Kwork: заказы вшиты в HTML страницы проектов ----------------

KWORK_PAGE = "https://kwork.ru/projects"


# Живое определение _extract_json_array — ниже, и оно единственное. 20.09.2026 здесь стоял дубль
# функции с тем же именем: Python брал нижнее, поэтому верхнее не вызывалось вообще — 43 строки
# мёртвого кода и ловушка для чтения (открывший файл сверху читал неработающий код).


class StructureChanged(Exception):
    """Страница источника изменилась: разбор не нашёл ожидаемую структуру.

    Отдельный тип нужен, чтобы «сайт поменял вёрстку» не выглядело как «заказов нет».
    """


def clean_kwork_text(text: str) -> str:
    """Kwork отдаёт в JSON сырые HTML-сущности (&laquo; &mdash; &rarr;) и мусор тегов.

    Без чистки они сыплются прямо в карточки Telegram как есть.
    """
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_json_array(page: str, marker: str) -> list | None:
    """Найти после marker ПОДХОДЯЩИЙ JSON-массив и разобрать его.

    Возвращает:
      * список словарей — разобрал и нашёл заказы;
      * пустой список — разобрал, но подходящего массива нет: **заказов правда нет**;
      * **None — не разобрал**: маркера нет, скобки не сходятся или ни одного массива
        разобрать не удалось. Это признак смены вёрстки.

    Разделение важно: раньше «не разобрал» и «заказов нет» давали одно и то же — пустой
    список, и монитор молчал о смене вёрстки.

    После маркера бывают пустые массивы (например, wants_20891_data:[]),
    поэтому перебираем все '[' и берём первый непустой массив словарей.
    """
    start = page.find(marker)
    if start == -1:
        return None
    pos = start
    parsed_any = False          # разобрали ли хоть один массив — отличает «пусто» от «сломалось»
    while True:
        open_br = page.find("[", pos)
        if open_br == -1:
            return [] if parsed_any else None
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(open_br, len(page)):
            ch = page[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return None
        try:
            data = json.loads(page[open_br:end + 1])
        except json.JSONDecodeError:
            data = None
        else:
            parsed_any = True
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
        pos = end + 1  # пустой/не тот массив — ищем следующий


def parse_kwork(html: str) -> list[dict]:
    """Разобрать страницу kwork.ru/projects: карточки заказов из wantsListData.

    Возвращает те же dict, что parse_rss: id, title, url, description, published, price.
    """
    wants = _extract_json_array(html, '"wantsListData"')
    if wants is None:
        raise StructureChanged(
            "на странице нет подходящего массива wantsListData — похоже, Kwork поменял вёрстку"
        )
    items: list[dict] = []
    for want in wants:
        if not isinstance(want, dict) or want.get("status") not in (None, "active"):
            continue
        want_id = str(want.get("id", "")).strip()
        title = clean_kwork_text(want.get("name") or "")
        if not want_id or not title:
            continue
        dates = want.get("wantDates") or {}
        created = clean_kwork_text(dates.get("dateCreate") or "")
        description = clean_kwork_text(want.get("description") or "")
        price = want.get("possiblePriceLimit") or want.get("priceLimit")
        try:
            price = int(float(str(price).replace(" ", "")))
        except (TypeError, ValueError):
            price = None
        category = str(want.get("category_id") or "").strip()
        offers = want.get("kwork_count")
        profile = (want.get("wantUserGetProfileUrl") or "").strip()
        url = f"{KWORK_PAGE}/{want_id}"  # проверено: /projects/<id> открывает карточку (→ /view)
        note = f"откликов уже: {offers}" if isinstance(offers, int) else ""
        items.append({
            "id": f"kwork:{want_id}",
            "title": title,
            "url": url,
            "description": description,
            "published": None,  # у Kwork тут текст вида «16 сентября» без времени
            "published_text": created,
            "price": price,
            "profile": profile,
            "note": note,
        })
    return items


# ---------------- freelance.ru: задания вшиты в HTML ленты ----------------

FREELANCERU_PAGE = "https://freelance.ru/task"   # /projects/ редиректит сюда (проверено 18.09.2026)

# Дата в ленте — абсолютная и в московском времени («17.09.2026 20:56» в атрибуте
# title). Зона фиксированная: сайт пишет время по Москве.
MSK = timezone(timedelta(hours=3))


def parse_freelanceru(html: str) -> list[dict]:
    """Разобрать ленту freelance.ru: карточки `task-card__title-link`.

    Возвращает те же dict, что parse_rss: id, title, url, description, published, price.
    `published` — реальная дата (МСК) из атрибута title, поэтому работает и возраст
    «N ч назад», и правило свежести max_age_days; `published_text` — та же дата текстом.
    """
    items: list[dict] = []
    blocks = re.split(r'(?=<a class="task-card__title-link")', html)
    for chunk in blocks:
        link = re.search(r'href="(?P<href>/task/view/\d+)"[^>]*title="(?P<alt>[^"]*)"', chunk)
        if not link:
            continue
        title_raw = re.search(r"</div>\s*<a[^>]*>\s*(?P<title>.*?)\s*</a>", chunk, re.DOTALL)
        title = strip_html(title_raw.group("title")) if title_raw else link.group("alt").strip()
        if not title:
            continue

        desc_raw = re.search(r'<p class="task-card__desc">(?P<desc>.*?)</p>', chunk, re.DOTALL)
        desc = strip_html(desc_raw.group("desc")) if desc_raw else ""

        # гонорар лежит в правой колонке карточки: «6 500 &#8381;»
        budget = ""
        side = chunk.find("task-card__side")
        if side != -1:
            segment = html_module.unescape(chunk[side:side + 800])
            found = re.search(r"([\d\s]{2,12})\s*₽", segment)
            if found:
                budget = re.sub(r"\s+", " ", found.group(1)).strip() + " ₽"

        published_text, published = "", None
        clock = re.search(r'title="(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})"', chunk)
        if clock:
            day, month, year, hour, minute = map(int, clock.groups())
            published_text = clock.group(0).replace('title="', "").rstrip('"')
            try:
                published = datetime(year, month, day, hour, minute, tzinfo=MSK)
            except ValueError:              # мусорная дата — карточку не теряем
                published = None

        digits = re.sub(r"\D", "", budget)
        price = int(digits) if digits else None
        items.append({
            "id": f"fru:{link.group('href').rsplit('/', 1)[-1]}",
            "title": title[:200],
            "url": "https://freelance.ru" + link.group("href"),
            "description": (desc + (" | Гонорар: " + budget if budget else ""))[:1500],
            "published": published,
            "published_text": published_text,
            "price": price,
            "profile": "",
            "note": "",
        })
    return items


# ---------------- kadrof.ru: биржа фриланса (лента с вакансиями) ----------------

KADROF_PAGE = "https://www.kadrof.ru/work"


def parse_kadrof(html: str) -> list[dict]:
    """Разобрать ленту kadrof.ru/work: блоки `post project-list`.

    Проверено живьём 18.09.2026: 10 карточек на странице, у каждой дата
    («17.09.2026 в 12:01», МСК), заголовок со ссылкой `/work/<id>`, часто бюджет
    («120000 ₽») и краткое описание. Лента СМЕШАННАЯ: рядом с заданиями лежат
    вакансии («полная занятость», «оклад»), поэтому источник держим выключенным —
    включается одной строкой в конфиге, когда решим брать и вакансии.
    """
    items: list[dict] = []
    blocks = re.split(r'(?=<div class="post project-list")', html)
    for chunk in blocks:
        title_match = re.search(
            r'<div class="project-title">\s*<h6><a href="(?P<href>/work/\d+)">(?P<title>[^<]+)</a>',
            chunk)
        if not title_match:
            continue
        title = strip_html(title_match.group("title"))
        if not title:
            continue

        budget = ""
        amount = re.search(r'<span class="fix-budget">(?P<sum>[^<]+)</span>', chunk)
        if amount:
            budget = strip_html(amount.group("sum"))
        digits = re.sub(r"\D", "", budget)

        published_text, published = "", None
        date_match = re.search(r'<div class="date">(?P<date>[^<]+)</div>', chunk)
        if date_match:
            published_text = strip_html(date_match.group("date"))
            clock = re.search(r"(\d{2})\.(\d{2})\.(\d{4})\s+в\s+(\d{2}):(\d{2})", published_text)
            if clock:
                day, month, year, hour, minute = map(int, clock.groups())
                try:
                    published = datetime(year, month, day, hour, minute, tzinfo=MSK)
                except ValueError:
                    published = None

        desc = ""
        body = re.search(r'<div class="clear"></div>(?P<text>.*?)<div class="project-info">',
                         chunk, re.DOTALL)
        if body:
            desc = strip_html(body.group("text"))

        poster = ""
        poster_match = re.search(r'<div class="poster"><a[^>]*>(?P<name>[^<]+)</a>', chunk)
        if poster_match:
            poster = strip_html(poster_match.group("name"))

        items.append({
            "id": f"kadrof:{title_match.group('href').rsplit('/', 1)[-1]}",
            "title": title[:200],
            "url": "https://www.kadrof.ru" + title_match.group("href"),
            "description": desc[:1500],
            "published": published,
            "published_text": published_text,
            "price": int(digits) if digits else None,
            "profile": poster,
            "note": "",
        })
    return items
