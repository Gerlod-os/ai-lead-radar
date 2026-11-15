#!/usr/bin/env python3
"""admin_core.py — ОДНО ядро админки: настройки, порог ИИ, статистика.

Зачем отдельный слой. Админок две (кнопочная в Telegram и локальная веб-панель),
а правда должна быть одна: один конфиг, одна база, одни числа. Если каждая панель
будет читать и писать сама, они разъедутся на первой же правке — и владелец получит
два разных ответа на вопрос «какой сейчас порог».

Правило: frontend ничего не знает про файлы. Он зовёт функции отсюда.
  admin_ui = "monitor" → кнопочный бот (по умолчанию)
  admin_ui = "parser"  → локальная веб-панель

Переключение — запись одного ключа в конфиг. Миграций нет: обе панели читают
одно и то же, поэтому переключение не ломает состояние.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "configs" / "monitor.json"

UI_MONITOR = "monitor"      # кнопочная админка в Telegram
UI_PARSER = "parser"        # локальная веб-панель
UI_CHOICES = (UI_MONITOR, UI_PARSER)

SCORE_CHOICES = (6, 7, 8)   # кнопки порога ИИ: три значения, больше не нужно


def load(path: Path | None = None) -> dict:
    """Прочитать конфиг. Нет файла — пустой словарь, а не падение."""
    target = Path(path) if path else CONFIG
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def sweep_tmp(target: Path) -> int:
    """Убрать недописанные `<имя>.*.tmp` рядом с файлом. Вернуть, сколько убрали.

    Зачем: атомарная запись делает временный файл и заменяет им настоящий. Если
    замену запретили (песочница, права, диск), временный файл остаётся навсегда —
    и так копится мусор: 15.11.2026 в configs/ накопилось 13 таких файлов.
    Чистим перед каждой записью, а не «когда-нибудь потом»: тогда мусор не растёт
    даже если замена снова не пройдёт.
    """
    target = Path(target)
    removed = 0
    try:
        for junk in target.parent.glob(target.name + ".*.tmp"):
            try:
                junk.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def save(cfg: dict, path: Path | None = None) -> None:
    """Записать конфиг атомарно: tmp рядом + замена. Файл либо старый, либо новый целиком.

    Атомарность здесь не украшение: веб-панель и бот могут писать одновременно, и
    половина JSON означала бы «настройки пропали» при следующем чтении.
    """
    target = Path(path) if path else CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    sweep_tmp(target)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


# ------------------------------------------------------------- настройки

def get_ui(cfg: dict) -> str:
    """Какой frontend сейчас выбран. Неизвестное значение — кнопочный бот."""
    value = str(cfg.get("admin_ui") or UI_MONITOR).strip().lower()
    return value if value in UI_CHOICES else UI_MONITOR


def switch_ui(path: Path | None = None) -> str:
    """Переключить админку и вернуть новое значение. Один ключ, без перезапуска."""
    cfg = load(path)
    new = UI_PARSER if get_ui(cfg) == UI_MONITOR else UI_MONITOR
    cfg["admin_ui"] = new
    save(cfg, path)
    return new


def set_ui(value: str, path: Path | None = None) -> str:
    """Поставить конкретную админку (для тестов и для веб-панели)."""
    if value not in UI_CHOICES:
        raise ValueError(f"неизвестная админка: {value!r} (можно {UI_CHOICES})")
    cfg = load(path)
    cfg["admin_ui"] = value
    save(cfg, path)
    return value


def get_min_score(cfg: dict) -> int:
    try:
        return int(cfg.get("llm_min_score", 7))
    except (TypeError, ValueError):
        return 7


def set_min_score(value: int, path: Path | None = None) -> int:
    """Поставить порог ИИ. За пределами 0–10 — обрезаем, а не падаем."""
    score = max(0, min(10, int(value)))
    cfg = load(path)
    cfg["llm_min_score"] = score
    save(cfg, path)
    return score


def step_price(delta: int, path: Path | None = None) -> int:
    """Изменить минимальную цену шагом (кнопки ±1000). Ниже нуля не уходит."""
    cfg = load(path)
    price = max(0, int(cfg.get("min_price", 0) or 0) + int(delta))
    cfg["min_price"] = price
    save(cfg, path)
    return price


def toggle_vacancies(path: Path | None = None) -> bool:
    cfg = load(path)
    cfg["show_vacancies"] = not bool(cfg.get("show_vacancies"))
    save(cfg, path)
    return cfg["show_vacancies"]


def toggle_source(name: str, path: Path | None = None) -> bool | None:
    """Вкл/выкл источник по имени. None — такого источника нет."""
    cfg = load(path)
    for source in cfg.get("sources", []):
        if source.get("name") == name:
            source["enabled"] = not source.get("enabled", True)
            save(cfg, path)
            return source["enabled"]
    return None


def add_word(word: str, path: Path | None = None) -> list[str]:
    """Добавить ключевое слово (без дублей, в нижнем регистре)."""
    cfg = load(path)
    words = list(cfg.get("keywords", []))
    word = (word or "").strip().lower()
    if word and word not in words:
        words.append(word)
        cfg["keywords"] = words
        save(cfg, path)
    return words


def remove_word(word: str, path: Path | None = None) -> list[str]:
    """Убрать ключевое слово."""
    cfg = load(path)
    word = (word or "").strip().lower()
    words = [w for w in cfg.get("keywords", []) if str(w).lower() != word]
    cfg["keywords"] = words
    save(cfg, path)
    return words


# ------------------------------------------------------------- статистика

def counters(conn) -> dict:
    """Счётчики банка одним запросом: сколько заказов, лидов, доставлено, ждёт отправки."""
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]          # noqa: E731
    return {
        "orders": one("SELECT COUNT(*) FROM orders WHERE status='active'"),
        "leads": one("SELECT COUNT(*) FROM llm_results WHERE status='ok' AND score >= 7"),
        "delivered": one("SELECT COUNT(*) FROM orders WHERE delivered = 1"),
        # «Ждёт отправки» — ровно то, что вернёт pending_delivery: оценено, лид, не доставлено.
        # Считать просто delivered=0 было бы враньём: туда попадают и неоцененные заказы,
        # которые никуда не ждут, а лежат до следующего цикла.
        "waiting": one("SELECT COUNT(*) FROM orders o JOIN llm_results l ON l.item_id = o.id"
                       " WHERE o.notified = 1 AND o.delivered = 0 AND l.status = 'ok'"
                       " AND l.score >= 7"),
        "llm_errors": one("SELECT COUNT(*) FROM llm_results WHERE status <> 'ok'"),
        "avg_score": round(one("SELECT COALESCE(AVG(score), 0) FROM llm_results WHERE status='ok'"), 1),
    }


def source_health(conn, cfg: dict) -> list[dict]:
    """Здоровье источников: последний прогон каждого (статус, позиции, ошибка)."""
    out = []
    for source in cfg.get("sources", []):
        row = conn.execute(
            "SELECT ts, ok, error, items, new_items FROM runs WHERE source = ?"
            " ORDER BY ts DESC LIMIT 1", (source["name"],)).fetchone()
        item = {"name": source["name"], "enabled": bool(source.get("enabled", True)),
                "type": source.get("type", "?"), "ts": "", "ok": None,
                "error": "", "items": 0, "new": 0, "silent_hours": None}
        if row:
            item.update(ts=row[0], ok=bool(row[1]), error=row[2] or "",
                        items=row[3] or 0, new=row[4] or 0)
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(row[0])
                item["silent_hours"] = round(age.total_seconds() / 3600, 1)
            except ValueError:
                pass
        out.append(item)
    return out


# ------------------------------------------------------------- расписание

def read_schedule(cfg: dict | None = None) -> dict:
    """Когда цикл запускается сам. Выключено по умолчанию — это тратит вызовы модели.

    Хранится в конфиге, чтобы обе админки видели одно и то же:
      enabled         — включено ли авто-расписание;
      interval_hours  — как часто (часы);
      last_run        — ISO время последнего автозапуска.
    """
    block = (cfg if cfg is not None else load()).get("schedule") or {}
    try:
        hours = float(block.get("interval_hours") or 6)
    except (TypeError, ValueError):
        hours = 6.0
    return {"enabled": bool(block.get("enabled")), "interval_hours": max(0.5, hours),
            "last_run": str(block.get("last_run") or "")}


def write_schedule(enabled: bool, interval_hours: float, last_run: str = "",
                   path: Path | None = None) -> dict:
    """Записать расписание в конфиг."""
    cfg = load(path)
    cfg["schedule"] = {"enabled": bool(enabled), "interval_hours": float(interval_hours),
                       "last_run": last_run or ""}
    save(cfg, path)
    return cfg["schedule"]


def mark_schedule_run(path: Path | None = None) -> None:
    """Отметить, что автозапуск только что сработал."""
    cfg = load(path)
    state = read_schedule(cfg)
    write_schedule(state["enabled"], state["interval_hours"],
                   datetime.now(timezone.utc).isoformat(timespec="seconds"), path)


def schedule_due(cfg: dict | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """Пора ли запускать цикл самому. Вернуть (пора, объяснение по-русски).

    Объяснение нужно, чтобы в панели было видно не «нет», а почему нет:
    выключено / прошло мало времени / ещё ни разу не запускалось.
    """
    state = read_schedule(cfg)
    if not state["enabled"]:
        return False, "расписание выключено — сбор только по кнопке"
    if not state["last_run"]:
        return True, "автозапуска ещё не было — запускаю"
    try:
        previous = datetime.fromisoformat(state["last_run"])
    except ValueError:
        return True, "время прошлого запуска испорчено — запускаю"
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    passed = (moment - previous).total_seconds() / 3600
    if passed >= state["interval_hours"]:
        return True, f"прошло {passed:.1f} ч из {state['interval_hours']:g} — пора"
    return False, f"прошло {passed:.1f} ч из {state['interval_hours']:g} — рано"


def next_run_at(cfg: dict | None = None, now: datetime | None = None) -> datetime | None:
    """Когда сработает в следующий раз. None — расписание выключено."""
    state = read_schedule(cfg)
    if not state["enabled"]:
        return None
    moment = now or datetime.now(timezone.utc)
    if not state["last_run"]:
        return moment
    try:
        previous = datetime.fromisoformat(state["last_run"])
    except ValueError:
        return moment
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=timezone.utc)
    return previous + timedelta(hours=state["interval_hours"])


def snapshot(conn, cfg: dict) -> dict:
    """Всё, что показывают обе админки, одним словарём — чтобы числа не разъезжались."""
    return {
        "admin_ui": get_ui(cfg),
        "min_score": get_min_score(cfg),
        "min_price": int(cfg.get("min_price", 0) or 0),
        "show_vacancies": bool(cfg.get("show_vacancies")),
        "keywords": list(cfg.get("keywords", [])),
        "counters": counters(conn),
        "sources": source_health(conn, cfg),
    }
