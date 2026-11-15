"""storage.py — банк данных заказов для job-monitor.

orders.db — источник правды (SQLite):
  orders — каждая карточка: площадка, источник, текст, цена, статус active/archived
  runs   — журнал циклов по источникам (здоровье источников, для статистики)
Рядом человекочитаемые выгрузки (по площадке, по месяцам):
  volume/data/<Площадка>/<ГГГГ-ММ>.jsonl — все новые карточки
  volume/data/_archive/<ГГГГ-ММ>.jsonl   — кто ушёл в архив и почему
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3

import budget
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ORDERS_DB = ROOT / "volume" / "orders.db"          # банк данных
DATA_DIR = ROOT / "volume" / "data"                # выгрузки по площадкам
LEGACY_DB = ROOT / "volume" / "state.db"           # старая база-дедуп (мигрируется сама)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  source_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'order',
  title TEXT, url TEXT, description TEXT,
  price INTEGER, published TEXT, note TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  -- notified = 1 значит «ПРОШЁЛ ФИЛЬТР НИШИ», а НЕ «доставлено в Telegram».
  -- Колонка стоит с 18.09 и переименование сломало бы bot.py и stats.py, поэтому
  -- имя оставлено, а смысл закреплён здесь. Доставку доказывает volume/heartbeat.json
  -- (поле sent/failed) и честная строка в monitor.log — не эта колонка.
  notified INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  archived_at TEXT, archive_reason TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  ts TEXT NOT NULL, source TEXT NOT NULL,
  items INTEGER, new_items INTEGER, ok INTEGER, error TEXT
);
-- Результаты ИИ лежат ОТДЕЛЬНОЙ таблицей, а не колонками в orders. Довод: сбор заказов
-- и оценка ИИ живут разными жизнями — модель можно переспросить, не трогая банк,
-- а 116 старых тестов и починка P0 остаются изолированными.
CREATE TABLE IF NOT EXISTS llm_results (
  item_id TEXT PRIMARY KEY,
  is_lead INTEGER NOT NULL DEFAULT 0,
  category TEXT, budget TEXT, summary TEXT,
  score INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  status TEXT NOT NULL,          -- ok | invalid_json | error
  attempts INTEGER NOT NULL DEFAULT 0,
  sheet_sent INTEGER NOT NULL DEFAULT 0,   -- строка уже ушла в Google Таблицу
  updated_at TEXT NOT NULL
);
-- Вьюха недель: одна дата «начало недели» (понедельник) на каждый заказ.
-- Без неё падал раздел «Статистика»: stats.py спрашивал v_orders_weekly,
-- а создавать её было некому — вьюха жила только в чьей-то старой базе.
-- Неделя считается от даты публикации, а если её нет — от первого появления.
-- Формула: (день недели %w + 6) % 7 даёт смещение до понедельника (вс=6, пн=0).
CREATE VIEW IF NOT EXISTS v_orders_weekly AS
SELECT
  date(COALESCE(NULLIF(published, ''), first_seen),
       '-' || ((CAST(strftime('%w', COALESCE(NULLIF(published, ''), first_seen)) AS INTEGER) + 6) % 7)
            || ' days') AS week_start,
  id, platform, source_name, kind, price, notified, status, published, first_seen
FROM orders;
"""

# Маркеры вакансии в тексте: найдено хоть одно → kind='vacancy' (не блокируем, а метим).
VACANCY_MARKS = (
    "вакансия", "вакансии", "в штат", "оклад", "зарплата", "зп ", " зп",
    "резюме", "требуется сотрудник", "ищем в команду", "трудоустройство",
    "полная занятость", "график работы", "оформление по тк", "испытательный срок",
    "full-time", "full time", "job opening", "we are hiring",
)


def classify_kind(title: str, description: str) -> str:
    """Заказ или вакансия? Вакансия — когда в тексте маркеры найма (оклад, в штат, резюме…)."""
    haystack = f"{title or ''} {description or ''}".lower()
    return "vacancy" if any(m in haystack for m in VACANCY_MARKS) else "order"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def platform_of(source_name: str) -> str:
    """Площадка по имени источника (если в конфиге не задана явно)."""
    if source_name.startswith("Kwork"):
        return "Kwork"
    if source_name.startswith("Freelance"):        # freelance.ru — HTML-лента, не FL.ru
        return "Freelance.ru"
    if source_name.startswith("Kadrof"):
        return "Kadrof"
    if "fl" in source_name.lower():
        return "FL.ru"
    return "Telegram"


def connect() -> sqlite3.Connection:
    ORDERS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ORDERS_DB)
    conn.executescript(SCHEMA)
    _ensure_kind_column(conn)
    _ensure_delivered_column(conn)
    _migrate_legacy(conn)
    _migrate_flat_archive()
    return conn


def _ensure_delivered_column(conn: sqlite3.Connection) -> None:
    """Колонка delivered: доказательство, что карточка РЕАЛЬНО ушла в чат.

    Зачем отдельно от notified: notified значит «прошёл фильтр ниши». Пока доставка
    не была отдельным полем, упавшая отправка теряла заказ навсегда — в банке он был,
    а в чате нет, и повторно не слался (P0 из плана, шаг 1).
    """
    cols = [row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "delivered" in cols:
        return
    conn.execute("ALTER TABLE orders ADD COLUMN delivered INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def _ensure_kind_column(conn: sqlite3.Connection) -> None:
    """Старый банк без колонки kind → добавляем и классифицируем всё, что уже лежит."""
    cols = [row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "kind" in cols:
        return
    conn.execute("ALTER TABLE orders ADD COLUMN kind TEXT NOT NULL DEFAULT 'order'")
    conn.commit()
    rows = conn.execute("SELECT id, title, description FROM orders").fetchall()
    for oid, title, desc in rows:
        conn.execute("UPDATE orders SET kind = ? WHERE id = ?", (classify_kind(title, desc), oid))
    conn.commit()
    print(f"[storage] колонка kind добавлена, классифицировано: {len(rows)} карточек")


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """Разово перенести старую базу-дедуп (state.db) в банк и переименовать её."""
    if not LEGACY_DB.exists():
        return
    try:
        old = sqlite3.connect(LEGACY_DB)
        rows = old.execute("SELECT id, source, title, url, first_seen, notified FROM seen").fetchall()
        old.close()
    except sqlite3.Error:
        return
    for id_, source, title, url, first_seen, notified in rows:
        conn.execute(
            "INSERT OR IGNORE INTO orders (id, platform, source_name, title, url, first_seen, last_seen, notified)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (id_, platform_of(source), source, title, url, first_seen, first_seen, notified),
        )
    conn.commit()
    shutil.move(str(LEGACY_DB), str(LEGACY_DB) + ".migrated")
    print(f"[storage] перенесено из state.db в банк: {len(rows)} карточек")


def is_new(conn: sqlite3.Connection, item_id: str) -> bool:
    return conn.execute("SELECT 1 FROM orders WHERE id = ?", (item_id,)).fetchone() is None


def upsert(conn: sqlite3.Connection, platform: str, source_name: str, item: dict) -> None:
    """Новая карточка → вставить (с меткой заказ/вакансия); знакомая → обновить поля и last_seen."""
    kind = classify_kind(item.get("title"), item.get("description"))
    conn.execute(
        "INSERT INTO orders (id, platform, source_name, kind, title, url, description, price, published, note,"
        " first_seen, last_seen, notified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET title=excluded.title, url=excluded.url,"
        " description=excluded.description, price=excluded.price, note=excluded.note, kind=excluded.kind,"
        " last_seen=excluded.last_seen",
        (item["id"], platform, source_name, kind, (item.get("title") or "")[:300], (item.get("url") or "")[:500],
         (item.get("description") or "")[:2000], item.get("price"),
         item["published"].isoformat(timespec="seconds") if item.get("published") else None,
         (item.get("note") or "")[:120] or None,
         now_iso(), now_iso(), 0),
    )
    conn.commit()


def mark_notified(conn: sqlite3.Connection, item_id: str, passed: bool) -> None:
    """Запомнить, что новую карточку обработали фильтром и (если прошла) показывали."""
    if passed:
        conn.execute("UPDATE orders SET notified = 1 WHERE id = ?", (item_id,))
        conn.commit()


# ---- ЧАСТЬ 2: выгрузки, архив, журнал ----
def dump_order(platform: str, item: dict, passed: bool, reason: str) -> None:
    """Новая карточка → файл выгрузки своей площадки: volume/data/<Площадка>/<ГГГГ-ММ>.jsonl."""
    folder = DATA_DIR / re.sub(r"[^\w]+", "-", platform, flags=re.UNICODE).strip("-")
    folder.mkdir(parents=True, exist_ok=True)
    row = {
        "seen_at": now_iso(),
        "platform": platform,
        "id": item["id"],
        "title": (item.get("title") or "")[:300],
        "url": (item.get("url") or "")[:500],
        "price": item.get("price"),
        "published": item["published"].isoformat(timespec="seconds") if item.get("published") else None,
        "passed": passed,
        "reason": reason,
        "description": (item.get("description") or "")[:1000],
    }
    with (folder / f"{datetime.now(timezone.utc):%Y-%m}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def archive_pass(conn: sqlite3.Connection, cfg: dict) -> list[dict]:
    """Отправить в архив неактивные заказы.

    Правила (настраиваются в конфиге, блок archive):
      week_days  — карточка старше стольких дней (по дате публикации, а если её нет —
                   по дате первой встречи) → архив, причина «старше N дней»;
      gone_hours — карточку не видели в ленте дольше стольких часов → архив,
                   причина «пропал из ленты» (заказ сняли/закрыли);
      keep       — исключения: подстроки id или url, которые не архивируем никогда.
    """
    rules = cfg.get("archive") or {}
    week = int(rules.get("week_days", 7))
    gone = int(rules.get("gone_hours", 48))
    keep = [str(k).lower() for k in rules.get("keep", []) if str(k).strip()]
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT id, platform, source_name, title, url, price, published, first_seen, last_seen"
        " FROM orders WHERE status = 'active'"
    ).fetchall()
    archived: list[dict] = []
    for id_, platform, source_name, title, url, price, published, first_seen, last_seen in rows:
        if any(k in id_.lower() or k in (url or "").lower() for k in keep):
            continue
        try:
            base = datetime.fromisoformat(published or first_seen)
        except ValueError:
            base = None
        try:
            seen = datetime.fromisoformat(last_seen)
        except ValueError:
            seen = None
        reason = None
        if base and now - base >= timedelta(days=week):
            reason = f"старше {week} дней"
        elif seen and now - seen >= timedelta(hours=gone):
            reason = f"пропал из ленты ({gone}+ ч)"
        if not reason:
            continue
        conn.execute(
            "UPDATE orders SET status = 'archived', archived_at = ?, archive_reason = ? WHERE id = ?",
            (now_iso(), reason, id_),
        )
        record = {
            "archived_at": now_iso(), "platform": platform, "source": source_name,
            "id": id_, "title": title, "url": url, "price": price, "reason": reason,
        }
        _append_archive(record)
        archived.append(record)
    if archived:
        conn.commit()
    return archived


def _archive_dir(platform: str) -> Path:
    """Папка архива одной площадки: volume/data/_archive/<Площадка>/."""
    folder = DATA_DIR / "_archive" / re.sub(r"[^\w]+", "-", platform, flags=re.UNICODE).strip("-")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _append_archive(record: dict) -> None:
    with (_archive_dir(record["platform"]) / f"{datetime.now(timezone.utc):%Y-%m}.jsonl").open(
        "a", encoding="utf-8"
    ) as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _migrate_flat_archive() -> None:
    """Разово разложить старый плоский архив (data/_archive/<мес>.jsonl) по площадкам."""
    flat_dir = DATA_DIR / "_archive"
    if not flat_dir.exists():
        return
    for flat in flat_dir.glob("*.jsonl"):
        moved = 0
        for line in flat.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not record.get("platform"):
                continue
            _append_archive(record)
            moved += 1
        if moved:
            flat.unlink()
            print(f"[storage] архив разложен по площадкам: {flat.name} → {moved} записей")


def run_log(conn: sqlite3.Connection, source: str, stats: dict) -> None:
    """Строка в журнал циклов: сколько скачали, сколько нового, была ли ошибка."""
    conn.execute(
        "INSERT INTO runs (ts, source, items, new_items, ok, error) VALUES (?,?,?,?,?,?)",
        (now_iso(), source, stats.get("items", 0), stats.get("new", 0),
         int(stats.get("error") is None), stats.get("error")),
    )
    conn.commit()


# ---- ЧАСТЬ 3: результаты ИИ, доставка, выгрузка ----

def save_llm(conn: sqlite3.Connection, item_id: str, result: dict) -> None:
    """Записать (или перезаписать) оценку ИИ по заказу.

    Одна строка на заказ — ключ item_id. Повторный вопрос модели перезаписывает
    ответ, а не плодит дубли: именно это проверяет критерий «ровно по строке на пост».
    """
    conn.execute(
        "INSERT INTO llm_results (item_id, is_lead, category, budget, summary, score,"
        " reason, status, attempts, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(item_id) DO UPDATE SET is_lead=excluded.is_lead,"
        " category=excluded.category, budget=excluded.budget, summary=excluded.summary,"
        " score=excluded.score, reason=excluded.reason, status=excluded.status,"
        " attempts=excluded.attempts, updated_at=excluded.updated_at",
        (item_id, int(bool(result.get("is_lead"))), result.get("category"),
         result.get("budget"), result.get("summary"), int(result.get("score") or 0),
         result.get("reason"), result.get("status") or "error",
         int(result.get("attempts") or 0), now_iso()),
    )
    conn.commit()


def get_llm(conn: sqlite3.Connection, item_id: str) -> dict | None:
    row = conn.execute(
        "SELECT is_lead, category, budget, summary, score, reason, status, attempts"
        " FROM llm_results WHERE item_id = ?", (item_id,)).fetchone()
    if not row:
        return None
    keys = ("is_lead", "category", "budget", "summary", "score", "reason", "status", "attempts")
    data = dict(zip(keys, row))
    data["is_lead"] = bool(data["is_lead"])
    return data


def set_delivered(conn: sqlite3.Connection, item_id: str, delivered: bool = True) -> None:
    """Отметить, что карточка дошла (или не дошла) до чата."""
    conn.execute("UPDATE orders SET delivered = ? WHERE id = ?",
                 (1 if delivered else 0, item_id))
    conn.commit()


def unevaluated(conn: sqlite3.Connection, limit: int = 25) -> list[dict]:
    """Заказы, которые прошли слова, но модель их ещё не видела.

    Зачем это нужно. За цикл к модели уходит не больше `max_llm_per_run` заказов,
    а источник за один проход отдаёт больше. Без добора такие заказы лежали бы в банке
    с notified=1 и без оценки — то есть «прошли фильтр и пропали». Добор закрывает
    эту дыру: следующий цикл сначала оценивает хвост, потом идёт за новыми.
    """
    rows = conn.execute(
        "SELECT o.id, o.source_name, o.title, o.description, o.url, o.price,"
        " o.published, o.first_seen"
        " FROM orders o LEFT JOIN llm_results l ON l.item_id = o.id"
        " WHERE o.notified = 1 AND l.item_id IS NULL AND o.status = 'active'"
        " ORDER BY o.first_seen DESC LIMIT ?", (int(limit),)).fetchall()
    keys = ("id", "source_name", "title", "description", "url", "price",
            "published", "first_seen")
    return [dict(zip(keys, row)) for row in rows]


def pending_delivery(conn: sqlite3.Connection, min_score: int, limit: int = 20) -> list[dict]:
    """Лиды, которые обязаны были уйти, но не ушли: прошли фильтр, оценены, не доставлены.

    Это и есть лечение P0: упавшая отправка больше не теряет заказ, а возвращается
    в очередь на следующем цикле. Берём только status='ok' — кривой ответ модели
    не должен превращаться в карточку с оценкой 0.
    """
    rows = conn.execute(
        "SELECT o.id, o.source_name, o.title, o.url, o.price,"
        " COALESCE(o.published, o.first_seen) AS when_seen,"
        " l.score, l.category, l.budget, o.price, l.summary"
        " FROM orders o JOIN llm_results l ON l.item_id = o.id"
        " WHERE o.notified = 1 AND o.delivered = 0 AND l.status = 'ok' AND l.score >= ?"
        " ORDER BY COALESCE(o.published, o.first_seen) DESC LIMIT ?",
        (int(min_score), int(limit)),
    ).fetchall()
    keys = ("id", "source_name", "title", "url", "price", "when_seen",
            "score", "category", "budget", "ad_price", "summary")
    return [dict(zip(keys, row)) for row in rows]


CSV_HEADER = ("Дата", "Источник", "Оценка", "Категория", "Бюджет", "Суть", "Ссылка")


def sheet_pending(conn: sqlite3.Connection, min_score: int, limit: int = 50) -> list[dict]:
    """Лиды, которых ещё нет в Google Таблице (поле sheet_sent = 0)."""
    rows = conn.execute(
        "SELECT o.id, COALESCE(o.published, o.first_seen), o.source_name, l.score,"
        " l.category, l.budget, o.price,"
        " COALESCE(l.summary, o.title), o.url"
        " FROM orders o JOIN llm_results l ON l.item_id = o.id"
        " WHERE l.status = 'ok' AND l.sheet_sent = 0 AND l.score >= ?"
        " ORDER BY l.score DESC, o.first_seen DESC LIMIT ?",
        (int(min_score), int(limit)),
    ).fetchall()
    keys = ("id", "date", "source", "score", "category", "budget", "price", "summary", "url")
    out = []
    for row in rows:
        item = dict(zip(keys, row))
        item["date"] = (item["date"] or "")[:16].replace("T", " ")
        # Без бюджета — не лид и не строка таблицы (слово владельца 15.11.2026).
        # Проверка стоит здесь, а не в вызывающем коде: иначе строка без суммы
        # попадала в таблицу мимо правила, которое действует для чата.
        if not budget.has_budget(item["budget"], item["price"]):
            continue
        out.append(item)
    return out


def mark_sheet_sent(conn: sqlite3.Connection, item_id: str) -> None:
    """Строка принята Таблицей — второй раз её не шлём."""
    conn.execute("UPDATE llm_results SET sheet_sent = 1 WHERE item_id = ?", (item_id,))
    conn.commit()


CSV_HEADER = ("Дата", "Источник", "Оценка", "Категория", "Бюджет", "Суть", "Ссылка")


def export_leads_csv(conn: sqlite3.Connection, path: Path | None = None) -> Path:
    """Выгрузить лиды в CSV: одна строка на заказ, колонки — как в README.

    Файл перезаписывается целиком каждый цикл: CSV — витрина, а не журнал.
    История живёт в orders.db. Кодировка utf-8-sig: Excel без неё показывает кракозябры.
    """
    import csv

    target = Path(path) if path else ROOT / "exports" / "leads.csv"
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT COALESCE(o.published, o.first_seen), o.source_name, l.score,"
        " l.category, l.budget, l.summary, o.url"
        " FROM orders o JOIN llm_results l ON l.item_id = o.id"
        " WHERE l.status = 'ok' ORDER BY l.score DESC, o.first_seen DESC"
    ).fetchall()
    # Дата в выгрузке — «2026-11-15 12:18», а не ISO с секундами и зоной:
    # в Таблице и Excel читают глазами, а не парсером.
    rows = [((str(row[0] or "")[:16].replace("T", " ")),) + tuple(row[1:]) for row in rows]
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)
    return target

