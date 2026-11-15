"""stats.py — статистика по банку заказов (volume/orders.db).

Запуск:  python3 stats.py           # отчёт в консоль
         python3 stats.py --json    # то же в JSON (для скриптов и дашбордов)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "volume" / "orders.db"

import html


def h(text) -> str:
    """Экранирование для HTML-режима Telegram (своя копия — без цикла импортов с ботом)."""
    return html.escape(str(text), quote=False)


# Тон-подсветка площадок в сводке (панель бота, render_html).
PLATFORM_TONES = {"FL.ru": "🔵", "Kwork": "🟠", "Telegram": "⚪️", "Habr Карьера": "🟣"}

# Анти-археология: заказ, у которого дата публикации сильно старше даты первой
# встречи (больше 7 дней) — это старый архив источника (урок У55: мёртвый канал
# отдавал посты 2023 года как свежие). В недельную статистику его не берём,
# иначе старые недели выглядят «живыми».
FRESH = (
    "(published IS NULL OR first_seen IS NULL"
    " OR julianday(first_seen) - julianday(published) < 7)"
)


def collect(conn: sqlite3.Connection) -> dict:
    report: dict = {}

    report["by_platform"] = [
        {"platform": p, "active": a, "archived": b}
        for p, a, b in conn.execute(
            "SELECT platform, SUM(status='active'), SUM(status='archived')"
            " FROM orders GROUP BY platform ORDER BY platform"
        )
    ]
    report["price"] = [
        {"platform": p, "with_price": n, "avg": round(avg), "min": lo, "max": hi}
        for p, n, avg, lo, hi in conn.execute(
            "SELECT platform, COUNT(price), AVG(price), MIN(price), MAX(price)"
            " FROM orders WHERE price IS NOT NULL GROUP BY platform ORDER BY platform"
        )
    ]
    report["new_by_day"] = [
        {"day": d, "new": n}
        for d, n in conn.execute(
            "SELECT substr(first_seen, 1, 10) AS day, COUNT(*) FROM orders"
            " GROUP BY day ORDER BY day DESC LIMIT 10"
        )
    ]
    report["by_week"] = [
        {
            "week_start": w,
            "orders": total,
            "niche": niche or 0,
            "share_pct": round(100 * (niche or 0) / total) if total else 0,
            "with_price": nwp or 0,
            "avg_price": round(avg_price) if avg_price else None,
            "avg_niche_price": round(avg_niche_price) if avg_niche_price else None,
        }
        for w, total, niche, nwp, avg_price, avg_niche_price in conn.execute(
            "SELECT week_start, COUNT(*), SUM(notified = 1),"
            " SUM(price IS NOT NULL), AVG(price), AVG(CASE WHEN notified = 1 THEN price END)"
            " FROM v_orders_weekly WHERE kind = 'order' AND " + FRESH + " GROUP BY week_start ORDER BY week_start"
        )
    ]
    report["by_week_platform"] = [
        {"week_start": w, "platform": p, "orders": total, "niche": niche or 0}
        for w, p, total, niche in conn.execute(
            "SELECT week_start, platform, COUNT(*), SUM(notified = 1)"
            " FROM v_orders_weekly WHERE kind = 'order' AND " + FRESH + ""
            " GROUP BY week_start, platform ORDER BY week_start, platform"
        )
    ]
    report["top_active_sources"] = [
        {"source": s, "active": n}
        for s, n in conn.execute(
            "SELECT source_name, COUNT(*) FROM orders WHERE status='active'"
            " GROUP BY source_name ORDER BY COUNT(*) DESC LIMIT 10"
        )
    ]
    report["kinds"] = [
        {"kind": k, "active": a, "archived": b}
        for k, a, b in conn.execute(
            "SELECT kind, SUM(status='active'), SUM(status='archived') FROM orders GROUP BY kind"
        )
    ]
    report["archive_reasons"] = [
        {"reason": r or "?", "count": n}
        for r, n in conn.execute(
            "SELECT archive_reason, COUNT(*) FROM orders WHERE status='archived' GROUP BY archive_reason"
        )
    ]
    health = conn.execute(
        "SELECT COUNT(*), SUM(ok = 0), MAX(ts) FROM runs"
    ).fetchone()
    report["runs_health"] = {"total": health[0], "errors": health[1] or 0, "last_run": health[2]}
    return report


def render(report: dict) -> str:
    lines = ["📊 БАНК ЗАКАЗОВ — сводка", ""]
    lines.append("Заказы и вакансии (активные / в архиве):")
    kinds = {"order": "🚨 заказы", "vacancy": "💼 вакансии"}
    for row in report["kinds"]:
        lines.append(f"  {kinds.get(row['kind'], row['kind']):<12} {row['active']:>4} / {row['archived']:>4}")
    lines.append("")
    lines.append("По площадкам (активные / в архиве):")
    for row in report["by_platform"]:
        lines.append(f"  {row['platform']:<10} {row['active']:>4} активных · {row['archived']:>4} в архиве")
    if report["price"]:
        lines.append("")
        lines.append("Цены (только у заказов с указанной ценой):")
        for row in report["price"]:
            lines.append(
                f"  {row['platform']:<10} {row['with_price']:>4} шт · средняя ~{row['avg']:,.0f} ₽"
                f" · от {row['min']:,} до {row['max']:,} ₽".replace(",", " ")
            )
    if report["new_by_day"]:
        lines.append("")
        lines.append("Новые заказы по дням (последние 10 дней с данными):")
        for row in report["new_by_day"]:
            lines.append(f"  {row['day']}  +{row['new']}")
    if report["by_week"]:
        lines.append("")
        lines.append("Новые заказы по неделям (неделя начинается с понедельника):")
        lines.append("  неделя     всего  проф.  доля  с ценой  ср. цена (все)  ср. цена (проф.)")
        for row in report["by_week"]:
            avg_all = f"~{row['avg_price']:,} ₽".replace(",", " ") if row["avg_price"] else "—"
            avg_niche = f"~{row['avg_niche_price']:,} ₽".replace(",", " ") if row["avg_niche_price"] else "—"
            lines.append(
                f"  {row['week_start']}  {row['orders']:>5}  {row['niche']:>5}  {row['share_pct']:>3}%"
                f"  {row['with_price']:>7}  {avg_all:>14}  {avg_niche:>15}"
            )
        lines.append("  профильные = прошли фильтр ниши (боты/автоматизация/AI), notified=1")
        lines.append("  заказы, опубликованные сильно раньше, чем мы их увидели (архив источника), не считаем")
    if report["top_active_sources"]:
        lines.append("")
        lines.append("Самые живые источники (активные заказы):")
        for row in report["top_active_sources"]:
            lines.append(f"  {row['source']:<22} {row['active']:>4}")
    if report["archive_reasons"]:
        lines.append("")
        lines.append("Архив по причинам:")
        for row in report["archive_reasons"]:
            lines.append(f"  {row['reason']:<30} {row['count']:>4}")
    h = report["runs_health"]
    lines.append("")
    lines.append(f"Здоровье: циклов {h['total']}, ошибок {h['errors']}, последний {h['last_run']}")
    return "\n".join(lines)
def render_html(report: dict) -> str:
    """HTML-версия сводки для бота: заголовки-блоки, живые строки, цифры и доли.

    Полоски «▮» убраны 18.09.2026 по требованию владельца («убери палки в
    статистике, выглядит ублюдски») — вместо длины полоски доля в процентах.
    Текстовая render() остаётся для CLI/stats.py — не лезет в Telegram.
    """
    n_active = sum(r["active"] for r in report["by_platform"])
    n_arch = sum(r["archived"] for r in report["by_platform"])
    out = ["📊 <b>Банк заказов</b>", ""]
    out.append("<b>Площадки</b> · активных / в архиве")
    total_active = n_active or 1
    for r in report["by_platform"]:
        share = round(100 * r["active"] / total_active)
        out.append(f'{PLATFORM_TONES.get(r["platform"], "🔹")} <b>{h(r["platform"])}</b> — '
                   f'{r["active"]} / {r["archived"]} · доля {share} %')
    out.append(f"\nВсего: <b>{n_active}</b> активных, <b>{n_arch}</b> в архиве")
    if report["price"]:
        out.append("\n<b>Цены</b> (где указаны)")
        for r in report["price"]:
            out.append(
                f'{PLATFORM_TONES.get(r["platform"], "🔹")} <b>{h(r["platform"])}</b>: '
                f'{r["with_price"]} шт · средняя <b>{r["avg"]:,} ₽</b> · '
                f'{r["min"]:,}–{r["max"]:,} ₽'.replace(",", " ")
            )
    if report["new_by_day"]:
        out.append("\n<b>Новые по дням</b>")
        for r in report["new_by_day"]:
            out.append(f'{r["day"][5:]} — <b>{r["new"]}</b> новых')
    if report["by_week"]:
        row0 = report["by_week"][0]
        out.append(
            f'\n<b>Профильные за неделю</b> (боты/автоматизация/AI): '
            f'<b>{row0["niche"]}</b> из {row0["orders"]}, доля {row0["share_pct"]} %'
        )
        if row0["avg_niche_price"]:
            out.append(f'Средняя цена профильных: <b>{row0["avg_niche_price"]:,} ₽</b>'.replace(",", " "))
    if report["top_active_sources"]:
        out.append("\n<b>Самые живые источники</b>")
        for r in report["top_active_sources"]:
            out.append(f'• {h(r["source"])} — {r["active"]}')
    if report["archive_reasons"]:
        out.append("\n<b>Архив по причинам</b>")
        for r in report["archive_reasons"]:
            out.append(f'• {h(r["reason"])} — {r["count"]}')
    hp = report["runs_health"]
    last = (hp["last_run"] or "?").replace("T", " ")[:16]
    out.append(
        f'\n<i>Циклов: {hp["total"]}, ошибок: {hp["errors"]} · последний прогон {h(last)} МСК</i>'
    )
    return "\n".join(out)



def weekly_csv(conn: sqlite3.Connection) -> str:
    """Недельная таблица в CSV (разделитель «;» — так открывают русский Excel/Numbers).

    week_start;platform;orders;niche_orders;with_price;avg_price;avg_niche_price
    — заказы недели по каждой площадке + сколько цен вытащено и какие средние.
    """
    rows = conn.execute(
        "SELECT week_start, platform, COUNT(*), SUM(notified = 1),"
        " COUNT(price), CAST(AVG(price) AS INT),"
        " CAST(AVG(CASE WHEN notified = 1 THEN price END) AS INT)"
        " FROM v_orders_weekly WHERE kind = 'order' AND " + FRESH + ""
        " GROUP BY week_start, platform ORDER BY week_start, platform"
    ).fetchall()
    lines = ["week_start;platform;orders;niche_orders;with_price;avg_price;avg_niche_price"]
    lines += [
        f"{w};{p};{t};{n or 0};{wp or 0};{ap if ap is not None else ''};{anp if anp is not None else ''}"
        for w, p, t, n, wp, ap, anp in rows
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Статистика по банку заказов")
    parser.add_argument("--json", action="store_true", help="вывести JSON вместо текста")
    parser.add_argument("--csv", action="store_true", help="выгрузка по неделям и площадкам в CSV (для Excel/Numbers)")
    args = parser.parse_args()

    if not DB.exists():
        print("Банка ещё нет — запусти monitor.py, он создаст volume/orders.db")
        return 1
    conn = sqlite3.connect(DB)
    try:
        report = collect(conn)
        csv_text = weekly_csv(conn)
    finally:
        conn.close()

    if args.csv:
        print(csv_text)
    elif args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
