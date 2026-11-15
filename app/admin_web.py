#!/usr/bin/env python3
"""admin_web.py — локальная веб-панель админки: http.server, без зависимостей.

Зачем вторая админка. Кнопочная в Telegram удобна с телефона, но в ней тесно:
список источников, слова, порог, журнал — это таблицы, а не ряд кнопок. Здесь то же
ядро (admin_core), те же данные, свои экраны: Статус · Источники ·
Ключевые слова · Порог ИИ · Экспорт · Запуск · Журнал.

Два правила, заложенные в устройство:
  1) Панель НЕ опрашивает Telegram. Два процесса на одном BOT_TOKEN конфликтуют
     (409 Conflict, обновления теряются) — поэтому веб-панель живёт только с базой
     и конфигом.
  2) Слушает только 127.0.0.1. Это локальная панель, а не сайт: наружу её не выставляем,
     пароля у неё нет.

Запуск:  python3 admin_web.py            # http://127.0.0.1:8765
         python3 admin_web.py --port 9000
"""
from __future__ import annotations

import argparse
import html
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import admin_core as core
import storage as st

APP = Path(__file__).resolve().parent
ROOT = APP.parent
DEFAULT_PORT = 8765

SCREENS = ("Статус", "Источники", "Ключевые слова", "Порог ИИ", "Расписание",
           "Экспорт", "Запуск", "Журнал")

STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; background:#14161a; color:#e7e9ee;
       font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color:#7cc4ff; text-decoration:none; }
a:hover { text-decoration:underline; }
header { padding:18px 24px 6px; border-bottom:1px solid #262a31; }
h1 { margin:0 0 4px; font-size:19px; font-weight:600; }
.sub { color:#8b93a1; font-size:13px; }
nav { display:flex; flex-wrap:wrap; gap:6px; padding:12px 24px 0; }
nav a { padding:7px 12px; border:1px solid #2b3038; border-radius:8px; color:#c9d1de;
        font-size:14px; background:#191c22; }
nav a.on { background:#24507a; border-color:#35719f; color:#fff; }
main { padding:20px 24px 60px; max-width:1000px; }
h2 { font-size:16px; font-weight:600; margin:22px 0 10px; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid #242830; }
th { color:#8b93a1; font-weight:500; font-size:13px; }
.on-t { color:#68d391; } .off-t { color:#8b93a1; } .bad { color:#f28b82; }
.cards { display:flex; flex-wrap:wrap; gap:12px; margin:8px 0 4px; }
.card { background:#191c22; border:1px solid #262a31; border-radius:10px;
        padding:12px 16px; min-width:130px; }
.card b { display:block; font-size:22px; font-weight:600; }
.card span { color:#8b93a1; font-size:12px; }
button, input[type=submit] { background:#24507a; color:#fff; border:1px solid #35719f;
        border-radius:8px; padding:7px 12px; font-size:14px; cursor:pointer; }
button.ghost { background:#20242b; border-color:#2f343d; color:#c9d1de; }
input[type=text] { background:#0f1115; color:#e7e9ee; border:1px solid #2f343d;
        border-radius:8px; padding:7px 10px; font-size:14px; min-width:220px; }
form { display:inline; }
.note { color:#8b93a1; font-size:13px; margin-top:8px; }
.tag { font-size:12px; padding:2px 8px; border-radius:20px; background:#232830; color:#a9b2c1; }
pre { background:#0f1115; border:1px solid #262a31; border-radius:10px; padding:12px;
      overflow:auto; font-size:13px; color:#c9d1de; }
"""


def esc(value) -> str:
    """Экранировать чужой текст (заголовки заказов, ошибки источников)."""
    return html.escape(str(value if value is not None else ""), quote=True)


def page(title: str, body: str, active: str = "") -> bytes:
    """Собрать страницу: шапка, меню экранов, содержимое."""
    menu = "".join(
        f'<a class="{"on" if name == active else ""}" href="{slug(name)}">{esc(name)}</a>'
        for name in SCREENS
    )
    document = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · ai-lead-radar</title><style>{STYLE}</style></head>
<body><header><h1>⚙️ ai-lead-radar — админка</h1>
<div class="sub">локальная панель · то же ядро, что у кнопочного бота</div></header>
<nav>{menu}</nav><main>{body}</main></body></html>"""
    return document.encode("utf-8")


def slug(name: str) -> str:
    """Адрес экрана по русскому названию (без внешних зависимостей)."""
    return {
        "Статус": "/", "Источники": "/sources", "Ключевые слова": "/words",
        "Порог ИИ": "/score", "Расписание": "/schedule", "Экспорт": "/export",
        "Запуск": "/run", "Журнал": "/log",
    }[name]


# ------------------------------------------------------------------ экраны

def screen_status(conn, cfg) -> bytes:
    snap = core.snapshot(conn, cfg)
    c = snap["counters"]
    cards = "".join(
        f'<div class="card"><b>{value}</b><span>{label}</span></div>'
        for label, value in (("активных заказов", c["orders"]), ("лидов ≥7", c["leads"]),
                             ("доставлено", c["delivered"]), ("ждёт отправки", c["waiting"]),
                             ("сбоев ИИ", c["llm_errors"]), ("средняя оценка", c["avg_score"]))
    )
    rows = "".join(
        f'<tr><td>{esc(s["name"])}</td>'
        f'<td class="{"on-t" if s["enabled"] else "off-t"}">{"вкл" if s["enabled"] else "выкл"}</td>'
        f'<td>{esc(s["items"])} поз · +{esc(s["new"])}</td>'
        f'<td class="{"bad" if s["ok"] is False else ""}">'
        f'{"—" if s["ok"] is None else ("OK" if s["ok"] else esc(s["error"]))}</td>'
        f'<td>{esc(s["ts"][11:16]) if s["ts"] else "не проверялся"}</td></tr>'
        for s in snap["sources"]
    )
    body = f"""
<h2>Сводка</h2><div class="cards">{cards}</div>
<p class="note">Админка: <span class="tag">{esc(snap["admin_ui"])}</span> ·
порог ИИ: <b>{snap["min_score"]}</b>/10 · мин. цена: {snap["min_price"]} ₽ ·
вакансии: {"показываю" if snap["show_vacancies"] else "скрыты"}</p>
<h2>Источники</h2>
<table><tr><th>Источник</th><th>Вкл</th><th>Последний прогон</th><th>Статус</th><th>Время (UTC)</th></tr>
{rows}</table>"""
    return page("Статус", body, "Статус")


def screen_sources(conn, cfg) -> bytes:
    snap = core.snapshot(conn, cfg)
    rows = "".join(
        f'<tr><td>{esc(s["name"])}</td><td>{esc(s["type"])}</td>'
        f'<td class="{"on-t" if s["enabled"] else "off-t"}">{"🟢 вкл" if s["enabled"] else "⚪️ выкл"}</td>'
        f'<td><form method="post" action="/toggle-source">'
        f'<input type="hidden" name="name" value="{esc(s["name"])}">'
        f'<button class="ghost">{"Выключить" if s["enabled"] else "Включить"}</button></form></td></tr>'
        for s in snap["sources"]
    )
    body = f"""<h2>Источники</h2>
<table><tr><th>Имя</th><th>Тип разбора</th><th>Состояние</th><th></th></tr>{rows}</table>
<p class="note">Выключенный источник не опрашивается вовсе. Правка пишется в общий
конфиг — кнопочный бот увидит её на следующем нажатии.</p>"""
    return page("Источники", body, "Источники")


def screen_words(conn, cfg) -> bytes:
    words = list(cfg.get("keywords", []))
    rows = "".join(
        f'<tr><td>{esc(word)}</td><td><form method="post" action="/del-word">'
        f'<input type="hidden" name="word" value="{esc(word)}">'
        f'<button class="ghost">Убрать</button></form></td></tr>' for word in words
    )
    body = f"""<h2>Ключевые слова ({len(words)})</h2>
<form method="post" action="/add-word">
  <input type="text" name="word" placeholder="новое слово, например: парсер">
  <input type="submit" value="Добавить">
</form>
<table style="margin-top:14px"><tr><th>Слово</th><th></th></tr>{rows}</table>
<p class="note">Слово ловится по началу: «бот» поймает «бота» и «боты», но не «работают».
Стоп-слова живут в configs/monitor.json — их правят реже, поэтому кнопок нет.</p>"""
    return page("Ключевые слова", body, "Ключевые слова")


def screen_score(conn, cfg) -> bytes:
    current = core.get_min_score(cfg)
    buttons = "".join(
        f'<form method="post" action="/set-score" style="display:inline">'
        f'<input type="hidden" name="score" value="{value}">'
        f'<button class="{"ghost" if value != current else ""}">{value}</button></form> '
        for value in core.SCORE_CHOICES
    )
    body = f"""<h2>Порог ИИ</h2>
<p>Сейчас карточка уходит в чат при оценке <b>{current}</b> и выше.</p>
<p>{buttons}</p>
<p class="note">Тот же порог меняют кнопки в боте (раздел «Фильтры»). Значение одно
на оба вида админки — это ключ <code>llm_min_score</code> в конфиге.</p>"""
    return page("Порог ИИ", body, "Порог ИИ")


def screen_schedule(conn, cfg) -> bytes:
    """Расписание: когда цикл запускается сам. Пресеты вместо ввода чисел.

    Почему пресеты: считать часы руками — работа для машины. Три кнопки покрывают
    почти все случаи. Всё состояние лежит в конфиге, поэтому кнопочный бот видит
    то же самое расписание, что и эта страница.
    """
    state = core.read_schedule(cfg)
    due, why = core.schedule_due(cfg)
    following = core.next_run_at(cfg)
    when = following.strftime("%d.%m.%Y %H:%M") if following else "—"
    hours = int(state["interval_hours"]) if state["interval_hours"] == int(state["interval_hours"]) else state["interval_hours"]
    plan = f"включено: каждые {hours} ч" if state["enabled"] else "выключено — сбор только по кнопке"
    previous = state["last_run"][:16].replace("T", " ") if state["last_run"] else "ещё не запускалось"

    presets = [("Выключить", "off"), ("Каждый час", "1"),
               ("Каждые 6 ч", "6"), ("Раз в сутки", "24")]
    buttons = ""
    for label, value in presets:
        active = (value == "off" and not state["enabled"]) or \
                 (value != "off" and state["enabled"] and float(value) == state["interval_hours"])
        buttons += (f'<form method="post" action="/schedule-preset" style="display:inline">'
                    f'<input type="hidden" name="preset" value="{value}">'
                    f'<button class="{"ghost" if not active else ""}">{label}</button></form> ')

    body = f"""<h2>Расписание</h2>
<p>Сейчас: <b>{esc(plan)}</b></p>
<p>Проверка прямо сейчас: <i>{esc(why)}</i><br>
Следующий запуск: <b>{esc(when)}</b><br>
Прошлый автозапуск: {esc(previous)}</p>
<p>{buttons}</p>
<form method="post" action="/schedule-now">
  <button>⏰ Проверить и запустить, если пора</button>
</form>
<p class="note">Автозапуск идёт, пока открыта эта панель: раз в минуту она смотрит,
не пора ли. Закроешь панель — расписание остановится, но следующее открытие
догонит пропущенное. Чтобы цикл шёл круглосуточно без открытой панели, нужен
автопилот (служба macOS) — это отдельный шаг, проси владельца.</p>"""
    return page("Расписание", body, "Расписание")


def screen_export(conn, cfg) -> bytes:
    path = st.export_leads_csv(conn)
    rows = max(0, len(path.read_text(encoding="utf-8").splitlines()) - 1)
    body = f"""<h2>Экспорт</h2>
<p>CSV обновлён только что: <b>{rows}</b> строк.</p>
<p><a href="/leads.csv">⬇️ Скачать leads.csv</a></p>
<p class="note">Колонки: Дата · Источник · Оценка · Категория · Бюджет · Суть · Ссылка.
Разделитель «;», кодировка UTF-8 с меткой — Excel открывает без кракозябр.
Google Таблица подключается отдельно: адрес вебхука в .env, скрипт в docs/sheet-webhook.gs.</p>"""
    return page("Экспорт", body, "Экспорт")


def screen_run(conn, cfg) -> bytes:
    body = """<h2>Запуск</h2>
<form method="post" action="/start-cycle">
  <button>▶️ Прогнать цикл сейчас</button>
</form>
<p class="note">Цикл идёт в отдельном процессе и не блокирует панель. Что он сделал —
видно в «Журнале» и в консоли, откуда запущена панель. Долгий цикл с моделью
занимает минуты: по одному вызову на заказ.</p>"""
    return page("Запуск", body, "Запуск")


def screen_log(conn, cfg) -> bytes:
    rows = conn.execute(
        "SELECT ts, source, items, new_items, ok, error FROM runs ORDER BY ts DESC LIMIT 40"
    ).fetchall()
    body_rows = "".join(
        f'<tr><td>{esc(ts[11:19])}</td><td>{esc(source)}</td><td>{esc(items)}</td>'
        f'<td>{esc(new)}</td><td class="{"on-t" if ok else "bad"}">{"OK" if ok else esc(error or "ошибка")}</td></tr>'
        for ts, source, items, new, ok, error in rows
    )
    body = f"""<h2>Журнал прогонов</h2>
<table><tr><th>Время (UTC)</th><th>Источник</th><th>Позиций</th><th>Новых</th><th>Итог</th></tr>
{body_rows or '<tr><td colspan="5">пусто</td></tr>'}</table>"""
    return page("Журнал", body, "Журнал")


# ------------------------------------------------------------------ сервер

class Panel(BaseHTTPRequestHandler):
    """Обработчик панели: GET — экраны и файл, POST — действия."""

    server_version = "ai-lead-radar-admin/1.0"

    def log_message(self, fmt, *args):        # не засоряем консоль запросами к CSS
        pass

    def _send(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, where: str = "/"):
        self.send_response(303)
        self.send_header("Location", where)
        self.end_headers()

    def do_GET(self):                                    # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        conn = st.connect()
        try:
            cfg = core.load()
            if path == "/leads.csv":
                data = st.export_leads_csv(conn).read_bytes()
                self._send(data, ctype="text/csv; charset=utf-8")
                return
            screens = {
                "/": screen_status, "/sources": screen_sources, "/words": screen_words,
                "/score": screen_score, "/schedule": screen_schedule,
                "/export": screen_export, "/run": screen_run,
                "/log": screen_log,
            }
            handler = screens.get(path)
            if not handler:
                self._send(page("Нет такой страницы", "<h2>404</h2>"), 404)
                return
            self._send(handler(conn, cfg))
        finally:
            conn.close()

    def do_POST(self):                                   # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        value = lambda key: (form.get(key) or [""])[0]    # noqa: E731
        if path == "/toggle-source":
            core.toggle_source(value("name"))
            self._redirect("/sources")
        elif path == "/add-word":
            core.add_word(value("word"))
            self._redirect("/words")
        elif path == "/del-word":
            core.remove_word(value("word"))
            self._redirect("/words")
        elif path == "/set-score":
            core.set_min_score(int(value("score") or 7))
            self._redirect("/score")
        elif path == "/schedule-preset":
            preset = value("preset")
            state = core.read_schedule()
            if preset == "off":
                core.write_schedule(False, state["interval_hours"], state["last_run"])
            else:
                try:
                    hours = float(preset)
                except (TypeError, ValueError):
                    hours = 6.0
                core.write_schedule(True, hours, state["last_run"])
            self._redirect("/schedule")
        elif path == "/schedule-now":
            due, _why = core.schedule_due()
            if due:
                core.mark_schedule_run()
                threading.Thread(target=_run_cycle, daemon=True).start()
            self._redirect("/schedule")
        elif path == "/start-cycle":
            threading.Thread(target=_run_cycle, daemon=True).start()
            self._send(page("Запуск", "<h2>Цикл запущен</h2>"
                                      '<p class="note">Смотри консоль панели и «Журнал».</p>', "Запуск"))
        else:
            self._send(page("Нет такой страницы", "<h2>404</h2>"), 404)


def scheduler_loop(stop: threading.Event, interval: int = 60) -> None:
    """Раз в минуту проверять, не пора ли запустить цикл.

    Работает только пока открыта панель — это честно написано на экране.
    Метку last_run ставим ДО запуска: иначе медленный цикл (5 минут при пустом
    интервале) успел бы запуститься второй раз на следующей проверке.
    """
    while not stop.wait(interval):
        try:
            due, why = core.schedule_due()
            if due:
                core.mark_schedule_run()
                print(f"расписание: запускаю цикл ({why})", flush=True)
                _run_cycle()
        except Exception as error:                     # noqa: BLE001
            print(f"расписание: сбой проверки ({error})", flush=True)


def _run_cycle() -> None:
    """Прогнать monitor.py отдельным процессом (панель при этом не замирает)."""
    subprocess.run([sys.executable, "monitor.py"], cwd=str(APP), timeout=1800)


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Поднять панель. host по умолчанию — только этот компьютер."""
    return ThreadingHTTPServer((host, port), Panel)


def main() -> int:
    parser = argparse.ArgumentParser(description="Локальная веб-админка радара")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1",
                        help="127.0.0.1 = только этот компьютер (так и задумано)")
    args = parser.parse_args()
    server = serve(args.port, args.host)
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(stop,), daemon=True).start()
    print(f"панель: http://{args.host}:{args.port}  (Ctrl+C — стоп)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nпанель остановлена")
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
