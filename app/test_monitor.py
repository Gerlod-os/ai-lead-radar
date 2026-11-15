"""test_monitor.py — автотесты ai-lead-radar: сбор, фильтр, ИИ, доставка, админки, выгрузки.

Запуск:  python3 test_monitor.py    (только Python, без сети и без Telegram)
Правило: тест обязан уметь упасть. Если сломать адаптер ИИ — набор краснеет
(это проверяется отдельной пробой: см. README, раздел «Как проверить, что тесты живые»).
"""
import sys
sys.path.insert(0, '.')

import copy
import io
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}: ждал {want!r}, получил {got!r}")


def ok(name, condition):
    """Проверка «истинно/ложно» — короче, чем check(..., True)."""
    check(name, bool(condition), True)


from monitor import (age_text, card, contains, contains_stop, too_old, passes_filter,
                     send_chunks)
from sources import clean_kwork_text, parse_freelanceru, parse_kadrof, parse_kwork, parse_rss
from storage import classify_kind, platform_of
import llm_filter

print("== классификация заказ/вакансия ==")
check("заказ-парсер", classify_kind("Нужен парсер цен", "сделать за 3 дня"), "order")
check("вакансия-оклад", classify_kind("Python-разработчик", "в штат, оклад 200 тыс"), "vacancy")
check("вакансия-резюме", classify_kind("Ищем в команду", "присылайте резюме"), "vacancy")

print("== чистка Kwork ==")
check("сущности", clean_kwork_text("Фестиваль &laquo;Звонкий берег&raquo; &mdash; 2 дня"),
      "Фестиваль «Звонкий берег» — 2 дня")
check("теги", clean_kwork_text("<p>Нужен <b>бот</b></p>"), "Нужен бот")

print("== freelance.ru: разбор ленты (вёрстка снята с живой страницы 18.09.2026) ==")
FRU_HTML = """
<a class="task-card__title-link" href="/task/view/10699" title="Написание отзывов">
                Написание отзывов            </a>
        <p class="task-card__desc">Написать отзыв к нашему проекту<br />Выплата на карту</p>
        <div class="task-card__side">
            <div class="task-card__budget-lbl">Гонорар</div>
            <div class="task-card__budget">
                <span class="bold">950 &#8381;</span> <span class="cost-unit">/ заказ</span></div>
        </div>
        <div class="task-card__foot">
            <span class="task-card__foot-item" title="18.09.2026 07:35">18.09.2026</span>
        </div>
<a class="task-card__title-link" href="/task/view/10450" title="Копирайтер">
                Копирайтер            </a>
        <p class="task-card__desc">Ищем копирайтера для текстов</p>
        <div class="task-card__side"><span class="bold">3 700 &#8381;</span></div>
        <div class="task-card__foot">
            <span class="task-card__foot-item" title="17.09.2026 19:46">17.09.2026</span>
        </div>
"""
fru = parse_freelanceru(FRU_HTML)
check("две карточки разобраны", len(fru), 2)
check("id — по номеру задания", fru[0]["id"], "fru:10699")
check("ссылка на карточку", fru[0]["url"], "https://freelance.ru/task/view/10699")
check("заголовок из атрибута", fru[0]["title"], "Написание отзывов")
check("гонорар стал ценой", fru[0]["price"], 950)
check("цена с пробелом разобрана", fru[1]["price"], 3700)
check("дата разобрана в datetime (МСК)", fru[0]["published"].strftime("%d.%m.%Y %H:%M"),
      "18.09.2026 07:35")
check("дата текстом для карточки", fru[0]["published_text"], "18.09.2026 07:35")
check("описание в карточке", "Написать отзыв" in fru[0]["description"], True)
check("пустая лента не падает", parse_freelanceru("<html></html>"), [])

print("== kadrof.ru: разбор ленты ==")
KADROF_HTML = """
<div class="post project-list">
    <div class="date">17.09.2026 в 12:01</div>
    <div class="project-title">
        <h6><a href="/work/111440">Менеджер по закупкам (удалённо)</a></h6>
        <div class="budget"><span class="fix-budget">120000&nbsp;</span></div>
    </div>
    <div class="clear"></div>
    ООО "Сфера" ищет двух закупщиков в свою команду.
    <div class="project-info">
        <div class="poster"><a href="/userprofile/view/205505">Ксения Маслова</a></div>
    </div>
</div>
<div class="post project-list">
    <div class="date">17.09.2026 в 00:28</div>
    <div class="project-title">
        <h6><a href="/work/111435">Написать музыкальную аранжировку по образцу</a></h6>
    </div>
    <div class="clear"></div>
    Написать подобие по характеру и ритму.
    <div class="project-info"></div>
</div>
"""
kad = parse_kadrof(KADROF_HTML)
check("две карточки разобраны", len(kad), 2)
check("id — по номеру задания", kad[0]["id"], "kadrof:111440")
check("ссылка на карточку", kad[1]["url"], "https://www.kadrof.ru/work/111435")
check("заголовок без тегов", kad[0]["title"], "Менеджер по закупкам (удалённо)")
check("бюджет стал ценой", kad[0]["price"], 120000)
check("нет бюджета — цена пустая", kad[1]["price"], None)
check("дата разобрана (МСК)", kad[0]["published"].strftime("%d.%m.%Y %H:%M"), "17.09.2026 12:01")
check("заказчик в профиле", kad[0]["profile"], "Ксения Маслова")
check("описание в карточке", "закупщиков" in kad[0]["description"], True)

print("== площадка по имени источника ==")
check("Kwork-дизайн → Kwork", platform_of("Kwork-дизайн"), "Kwork")
check("Freelance.ru → Freelance.ru, а не FL.ru", platform_of("Freelance.ru"), "Freelance.ru")
check("Kadrof → Kadrof", platform_of("Kadrof"), "Kadrof")
check("FL.ru остаётся FL.ru", platform_of("FL.ru"), "FL.ru")

print("== правило свежести ==")
NOW = datetime.now(timezone.utc)
check("вчерашняя карточка свежая", too_old({"published": NOW - timedelta(hours=5)}, 7), False)
check("трёхлетняя карточка — архив", too_old({"published": NOW - timedelta(days=1201)}, 7), True)
check("на границе 7 суток ещё свежая",
      too_old({"published": NOW - timedelta(days=6, hours=23)}, 7), False)
check("правило выключено (0) — архив проходит",
      too_old({"published": NOW - timedelta(days=1201)}, 0), False)
check("дата неизвестна (Kwork) — карточка проходит", too_old({"published": None}, 7), False)
check("дата без зоны не роняет проверку", too_old({"published": datetime(2026, 1, 1)}, 7), True)
check("возраст в часах", age_text(NOW - timedelta(hours=5)).endswith("ч"), True)
check("возраст в днях, а не в часах", age_text(NOW - timedelta(days=1201)), "1201 дн")

print("== фильтр слов ==")
cfg_words = {"keywords": ["бот", "парсер"], "exclude_words": [], "min_price": 0,
             "show_vacancies": False}
check("наше проходит", passes_filter({"title": "Нужен бот", "description": ""}, cfg_words)[0], True)
check("вакансия отсеяна тумблером",
      passes_filter({"title": "Python-разработчик в штат", "description": "оклад, боты"},
                    cfg_words)[0], False)
check("вакансия при вкл тумблере",
      passes_filter({"title": "Python-разработчик в штат", "description": "оклад, боты"},
                    dict(cfg_words, show_vacancies=True))[0], True)
check("мусор не проходит",
      passes_filter({"title": "Дизайн сайта", "description": ""}, cfg_words)[0], False)
check("слово по началу: бот ловит ботов", contains("бот", "нужен ботов"), True)
check("слово по началу не ловит «работают»", contains("бот", "работают"), False)
check("стоп-слово не ловится по началу (seoshop)",
      contains_stop("seo", "seoshop"), False)
check("стоп-слово ловится как отдельное слово в составе через дефис",
      contains_stop("seo", "seo-продвижение"), True)

print("== карточка ==")
item = {"title": " Бот ", "description": "сделать", "price": 5000, "published": None,
        "published_text": "сегодня", "url": "https://x", "note": "", "profile": ""}
check("заказ с 🚨", card("FL", item)[:1], "🚨")
check("вакансия с 💼", card("FL", dict(item, title="Python в штат, оклад"))[:1], "💼")
with_llm = card("FL", item, llm={"score": 8, "category": "бот", "budget": "5 000",
                                 "summary": "сделать бота", "reason": "прямое совпадение"})
ok("в карточке есть оценка", "Оценка 8/10" in with_llm)
ok("в карточке есть суть", "сделать бота" in with_llm)
ok("карточка без ИИ показывает описание", "сделать" in card("FL", item))

print("== сводка без полосок ==")
from stats import render_html

REPORT = {
    "by_platform": [{"platform": "FL.ru", "active": 6, "archived": 2},
                    {"platform": "Kwork", "active": 3, "archived": 1}],
    "price": [{"platform": "FL.ru", "with_price": 4, "avg": 12000, "min": 3000, "max": 40000}],
    "new_by_day": [{"day": "2026-09-18", "new": 5}, {"day": "2026-09-17", "new": 2}],
    "by_week": [], "top_active_sources": [], "archive_reasons": [],
    "runs_health": {"total": 12, "errors": 0, "last_run": "2026-09-18T06:00:00+00:00"},
}
summary = render_html(REPORT)
check("в сводке нет полосок", chr(0x25AE) in summary or "|" in summary, False)
ok("площадка с долей в процентах", "FL.ru</b> — 6 / 2 · доля 67 %" in summary)
ok("здоровье прогонов на месте", "Циклов: 12, ошибок: 0" in summary)

print("== ИИ: чистка ответа ==")
check("баннер CLI срезан",
      llm_filter.strip_noise('> build · big-pickle\n\n{"score": 7}'), '{"score": 7}')
check("<think> срезан",
      llm_filter.strip_noise('<think>думаю</think>{"score": 7}'), '{"score": 7}')
check("markdown-забор срезан",
      llm_filter.strip_noise('```json\n{"score": 7}\n```').replace("\n", ""), '{"score": 7}')
check("JSON из болтовни", llm_filter.extract_json('Вот ответ: {"score": 7} — всё'), {"score": 7})
check("кривой JSON — None", llm_filter.extract_json('{"score": '), None)
check("пустой ответ — None", llm_filter.extract_json(""), None)
check("JSON-массив не принимается за объект", llm_filter.extract_json("[1,2,3]"), None)

print("== ИИ: проверка полей ==")
check("оценка строкой", llm_filter.normalize({"score": "7"})["score"], 7)
check("оценка с дробью округляется", llm_filter.normalize({"score": 6.6})["score"], 7)
check("оценка «7/10»", llm_filter.normalize({"score": "7/10"})["score"], 7)
check("оценка выше 10 обрезается", llm_filter.normalize({"score": 42})["score"], 10)
check("оценка ниже 0 обрезается", llm_filter.normalize({"score": -5})["score"], 0)
check("без оценки статус invalid_json",
      llm_filter.normalize({"summary": "x"})["status"], "invalid_json")
check("оценка 8 — лид", llm_filter.normalize({"score": 8})["is_lead"], True)
check("оценка 3 — не лид", llm_filter.normalize({"score": 3})["is_lead"], False)
check("поля обрезаются по длине",
      len(llm_filter.normalize({"score": 5, "summary": "я" * 900})["summary"]), 300)
check("None на входе — invalid_json", llm_filter.normalize(None)["status"], "invalid_json")

print("== категории: восемь канонов вместо свободного текста ==")
import categories as cat

check("канонов восемь", len(cat.CATEGORIES), 8)
check("канон сам в себя и не меняется", [cat.normalize(name) for name in cat.CATEGORIES],
      list(cat.CATEGORIES))
ok("вся таблица архива ведёт в канон",
   all(value in cat.CATEGORIES for value in cat.ARCHIVE.values()))
check("архив: бот с таблицей → Telegram-боты",
      cat.normalize("Telegram-бот + Google-таблица"), cat.BOTS)
check("архив: Senler → маркетинг, хотя в тексте есть «бот»",
      cat.normalize("настройка Senler, чат-боты, лендинг, интеграция платежей"), cat.MARKETING)
check("архив: инвайтинг → рутина, хотя в тексте есть «Telegram»",
      cat.normalize("Массовый инвайтинг пользователей в Telegram-чат"), cat.ROUTINE)
check("архив: парсинг с интеграцией → парсинг",
      cat.normalize("автоматизация и парсинг с интеграцией (Яндекс Диск API + Pla"), cat.PARSING)
check("новый свободный текст решается по словам",
      cat.normalize("нужен телеграм-бот с оплатой"), cat.BOTS)
check("непонятное → Другое", cat.normalize("выгулять собаку"), cat.OTHER)
check("пусто → Другое", cat.normalize(None), cat.OTHER)
check("пробелы и регистр не мешают", cat.normalize("  telegram-боты "), cat.BOTS)
ok("в промпте перечислены все восемь",
   all(name in llm_filter.PROMPT_DEFAULT for name in cat.CATEGORIES))
ok("промпт требует только эти значения", "строго одно из восьми" in llm_filter.PROMPT_DEFAULT)
check("модель написала своё — всё равно канон",
      llm_filter.normalize({"score": 9, "category": "нужен парсер с выгрузкой"})["category"],
      cat.PARSING)
check("модель не написала категорию — «Другое»",
      llm_filter.normalize({"score": 9})["category"], cat.OTHER)

print("== ИИ: промпт ==")
prompt = llm_filter.build_prompt({"title": "Нужен бот", "description": "для магазина"}, {})
ok("в промпте есть заголовок", "Нужен бот" in prompt)
ok("в промпте есть описание", "для магазина" in prompt)
check("свой шаблон из конфига работает",
      llm_filter.build_prompt({"title": "X", "description": ""}, {"llm_prompt": "Своё: {text}"}),
      "Своё: X")
check("шаблон без {text} не теряет заказ",
      llm_filter.build_prompt({"title": "X", "description": ""},
                              {"llm_prompt": "Оцени"}).endswith("X"), True)

print("== ИИ: режим CLI (подменённый вызов, без сети) ==")
captured = {}


def fake_popen(args, **kwargs):
    """Подмена самого процесса: обёртка работает через Popen, а не subprocess.run.

    Подменять run бессмысленно — вызов ушёл бы в настоящий /bin/echo, что уже
    один раз случилось и дало «в ответе аргументы команды».
    """
    captured["args"] = args
    captured["kwargs"] = kwargs
    captured["workdir_exists"] = os.path.isdir(args[3])

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return '{"score": 9, "summary": "бот"}', ""

        def kill(self):
            captured["killed"] = True

    return FakeProc()


real_popen = llm_filter.subprocess.Popen
llm_filter.subprocess.Popen = fake_popen
text, error = llm_filter.call_cli("промпт", {}, "/bin/echo", "model-x", "http://127.0.0.1:1", 5)
check("CLI: текст ответа получен", text, '{"score": 9, "summary": "бот"}')
check("CLI: ошибки нет", error, "")
check("CLI: аргументы — список, без shell", isinstance(captured["args"], list), True)
check("CLI: промпт — последний аргумент", captured["args"][-1], "промпт")
ok("CLI: логи печатаются в stderr (иначе причину отказа не увидеть)",
   "--print-logs" in captured["args"])
check("CLI: рабочая папка была жива на момент вызова", captured["workdir_exists"], True)
ok("CLI: рабочая папка удалена после вызова", not os.path.isdir(captured["args"][3]))
ok("CLI: deny-конфиг передан в окружении",
   '"bash":"deny"' in captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"].replace(" ", ""))
check("CLI: прокси выставлен как HTTP",
      captured["kwargs"]["env"].get("HTTPS_PROXY"), "http://127.0.0.1:1")
check("CLI: NO_PROXY закрывает локальные адреса",
      captured["kwargs"]["env"].get("NO_PROXY"), "localhost,127.0.0.1")
llm_filter.subprocess.Popen = real_popen
check("CLI: нет --auto (он разрешает всё)", "--auto" in captured["args"], False)
check("CLI: модель из настроек, а не из кода", captured["args"][captured["args"].index("--model") + 1],
      "model-x")

def silent_process(returncode: int, out: str, err: str):
    """Процесс, который ничего не печатает и сразу завершается с нужным кодом."""
    class Proc:
        def __init__(self):
            self.returncode = returncode

        def communicate(self, timeout=None):
            return out, err

        def kill(self):
            pass

    return lambda *a, **k: Proc()


llm_filter.subprocess.Popen = silent_process(2, "", "нет модели")
check("CLI: ненулевой код — ошибка с текстом",
      llm_filter.call_cli("p", {}, "/bin/echo", "m", "", 1), ("", "нет модели"))

llm_filter.subprocess.Popen = silent_process(0, "понг\n", "")
check("CLI: успешный код — ответ без ошибки",
      llm_filter.call_cli("p", {}, "/bin/echo", "m", "", 1), ("понг\n", ""))
llm_filter.subprocess.Popen = real_popen

print("== ИИ: режим openai_http на локальном стабе ==")


class Stub(BaseHTTPRequestHandler):
    """Локальный «чужой провайдер»: 200 / 401 / 429 / медленно / мусор."""

    mode = "ok"
    seen = {}

    def log_message(self, *args):
        pass

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        Stub.seen = {"path": self.path, "body": body,
                     "auth": self.headers.get("Authorization")}
        if Stub.mode == "401":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        if Stub.mode == "429":
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error":"rate limit"}')
            return
        if Stub.mode == "slow":
            time.sleep(1.2)
        answer = {"choices": [{"message": {"content": '{"score": 8, "summary": "сайт"}'}}]}
        if Stub.mode == "garbage":
            answer = {"choices": []}
        payload = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}/v1"

Stub.mode = "ok"
text, error = llm_filter.call_http("привет", {}, base, "test-key", "model-test", 5)
check("HTTP: текст ответа получен", text, '{"score": 8, "summary": "сайт"}')
check("HTTP: адрес /chat/completions", Stub.seen["path"], "/v1/chat/completions")
check("HTTP: ключ ушёл в заголовке", Stub.seen["auth"], "Bearer test-key")
check("HTTP: модель из настроек", Stub.seen["body"]["model"], "model-test")
check("HTTP: температура 0", Stub.seen["body"]["temperature"], 0)

Stub.mode = "401"
text, error = llm_filter.call_http("п", {}, base, "k", "m", 5)
check("HTTP 401 — ошибка вызова", (text, error.startswith("HTTP 401")), ("", True))
Stub.mode = "429"
check("HTTP 429 — ошибка вызова",
      llm_filter.call_http("п", {}, base, "k", "m", 5)[1].startswith("HTTP 429"), True)
Stub.mode = "slow"
check("HTTP таймаут ловится",
      "таймаут" in llm_filter.call_http("п", {}, base, "k", "m", 0.4)[1], True)
Stub.mode = "garbage"
check("HTTP мусор — понятная ошибка",
      "не похож на OpenAI" in llm_filter.call_http("п", {}, base, "k", "m", 5)[1], True)
check("HTTP: без адреса — ошибка, а не падение",
      llm_filter.call_http("п", {}, "", "k", "m", 5),
      ("", "не задан LLM_BASE_URL или LLM_MODEL"))
check("HTTP: ключ с кириллицей объясняется словами",
      llm_filter.call_http("п", {}, base, "ключ", "m", 5),
      ("", "LLM_API_KEY содержит не-ASCII символы — проверь копипаст"))
server.shutdown()

print("== ИИ: полный проход qualify (повтор и статусы) ==")
calls = {"n": 0}


def flaky_ask(prompt, env, conf=None):
    calls["n"] += 1
    if calls["n"] == 1:
        return '{"score": ', "", "opencode_cli"          # первый раз кривой JSON
    return '{"score": 8, "summary": "бот"}', "", "opencode_cli"


real_ask = llm_filter.ask
llm_filter.ask = flaky_ask
res = llm_filter.qualify({"title": "x", "description": ""}, {}, {}, sleep=lambda _s: None)
check("кривой JSON → повтор → ok", res["status"], "ok")
check("попыток записано 2", res["attempts"], 2)

llm_filter.ask = lambda p, e, c=None: ("", "HTTP 429", "openai_http")
res = llm_filter.qualify({"title": "x", "description": ""}, {}, {}, sleep=lambda _s: None)
check("два отказа подряд → status=error", res["status"], "error")
ok("причина отказа сохранена", "429" in res["reason"])
check("у отказа нет оценки-обманки", res["score"], 0)
llm_filter.ask = real_ask

print("== нет хардкода адреса, ключа, модели и прокси ==")
APP = Path(__file__).resolve().parent
sources_text = "\n".join(
    p.read_text(encoding="utf-8") for p in APP.glob("*.py") if p.name != "test_monitor.py")
check("нет адреса туннеля", "127.0.0.1:2082" in sources_text, False)
check("нет захардкоженной модели", "big-pickle" in sources_text, False)
# Адрес схемы в deny-конфиге — это не адрес запроса, а имя схемы конфигурации CLI.
# Убираем ровно эту строку и смотрим, не осталось ли адресов ещё где-то.
no_schema = sources_text.replace('"$schema": "https://opencode.ai/config.json"', "")
check("нет адреса шлюза в коде запросов", "opencode.ai" in no_schema, False)
import re as _re
check("нет ключа в коде", _re.search(r"sk-[A-Za-z0-9]{20,}", sources_text), None)
ok("прокси берётся из окружения",
   "LLM_PROXY_URL" in (APP / "llm_filter.py").read_text(encoding="utf-8"))
ok("режим openai_http объявлен в коде", '"openai_http"' in sources_text)

print("== отказы модели называются словами ==")

check("лимит бесплатных моделей — понятная причина",
      llm_filter.explain_error("ERROR stream error: AI_APICallError: Free usage limit reached."),
      "исчерпан бесплатный лимит моделей OpenCode — смени модель или подключи свой ключ")
check("v1 без бесплатных моделей",
      llm_filter.explain_error("This version no longer supports free models"),
      "эта версия OpenCode больше не поддерживает бесплатные модели (v1) — нужен v2 или ключ")
check("запрос из чужого клиента",
      llm_filter.explain_error("Free-tier chat models are only be used from within OpenCode"),
      "бесплатные модели OpenCode доступны только внутри их клиента — нужен свой ключ")
check("403 из-за запрета инструментов",
      llm_filter.explain_error("403 permission error"),
      "модель отвергла запрос из-за запрета инструментов (permission)")
check("401 — про ключ", llm_filter.explain_error("Unauthorized: bad key"),
      "ключ не принят (401) — проверь LLM_API_KEY")
check("неизвестный текст проходит как есть",
      llm_filter.explain_error("что-то своё"), "что-то своё")
check("пустой текст — пустая причина", llm_filter.explain_error(""), "")
# Сплошной запрет чтения у бесплатных моделей OpenCode даёт ложный «403 free tier».
# Нам он не нужен: рабочая папка пустая. Проверка сторожит, чтобы его не вернули.
ok("чтение НЕ запрещено в deny-конфиге", '"read"' not in llm_filter.DENY_CONFIG)

# Фальшивый клиент вместо opencode: проверяем поведение обёртки без сети.
fake_dir = Path(tempfile.mkdtemp(prefix="fake-cli-"))


def make_cli(name: str, body: str):
    path = fake_dir / name
    path.write_text("#!/usr/bin/env python3\nimport sys, time\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


hang = make_cli("hang.py", (
    "sys.stderr.write('ERROR AI_APICallError: Free usage limit reached.\\n')\n"
    "sys.stderr.flush()\n"
    "time.sleep(30)\n"))
_t0 = time.time()
text, err = llm_filter.call_cli("пинг", {}, hang, "модель", "", 2)
_elapsed = time.time() - _t0
check("висящий клиент обрывается по таймауту", text, "")
ok("и в причине — не «таймаут», а лимит", "исчерпан бесплатный лимит" in err)
ok("ожидание не длиннее таймаута", _elapsed < 8)

okay = make_cli("okay.py", "print('понг')\n")
check("нормальный ответ возвращается как есть",
      llm_filter.call_cli("пинг", {}, okay, "модель", "", 5), ("понг\n", ""))

# Клиент, который не знает флагов логирования: первый запуск падает, второй без них.
picky = make_cli("picky.py", (
    "if '--print-logs' in sys.argv:\n"
    "    sys.stderr.write('unknown option \\'--print-logs\\'\\n')\n"
    "    sys.exit(2)\n"
    "print('понг')\n"))
check("незнакомые флаги не ломают вызов — повтор без них",
      llm_filter.call_cli("пинг", {}, picky, "модель", "", 5), ("понг\n", ""))

print("== банк: одна строка на заказ, доставка, выгрузки ==")
import storage as st
import sheet_webhook

TMP = Path(tempfile.mkdtemp())
st.ORDERS_DB = TMP / "orders.db"
st.DATA_DIR = TMP / "data"
conn = st.connect()
st.upsert(conn, "FL.ru", "FL.ru", {"id": "a:1", "title": "Бот", "description": "нужен бот",
                                   "url": "u", "published": NOW, "price": 5000})
st.save_llm(conn, "a:1", {"score": 8, "category": "бот", "budget": "5000", "summary": "сделать",
                          "reason": "ок", "status": "ok", "attempts": 1})
st.save_llm(conn, "a:1", {"score": 9, "category": "бот", "budget": "5000", "summary": "сделать",
                          "reason": "ок", "status": "ok", "attempts": 2})
rows = conn.execute("SELECT COUNT(*) FROM llm_results").fetchone()[0]
check("повторная оценка не плодит дубль", rows, 1)
check("сохранён последний ответ", st.get_llm(conn, "a:1")["score"], 9)

st.mark_notified(conn, "a:1", True)
check("до отправки карточка ждёт доставки", len(st.pending_delivery(conn, 7)), 1)
st.set_delivered(conn, "a:1", True)
check("после доставки очередь пуста", len(st.pending_delivery(conn, 7)), 0)
st.set_delivered(conn, "a:1", False)
check("упавшая отправка возвращается в очередь (P0)", len(st.pending_delivery(conn, 7)), 1)
check("в очереди есть время для возраста карточки",
      bool(st.pending_delivery(conn, 7)[0]["when_seen"]), True)

st.upsert(conn, "FL.ru", "FL.ru", {"id": "a:2", "title": "Рерайт", "description": "текст",
                                   "url": "u2", "published": NOW, "price": 1000})
st.save_llm(conn, "a:2", {"score": 3, "status": "ok", "attempts": 1})
st.mark_notified(conn, "a:2", True)
check("низкая оценка не считается лидом", len(st.pending_delivery(conn, 7)), 1)
check("порог 3 поднимает и её", len(st.pending_delivery(conn, 3)), 2)

csv_path = st.export_leads_csv(conn, TMP / "leads.csv")
csv_text = csv_path.read_text(encoding="utf-8-sig")
check("CSV: колонки как в README", csv_text.splitlines()[0],
      "Дата;Источник;Оценка;Категория;Бюджет;Суть;Ссылка")
check("CSV: строка на каждый оценённый заказ", len(csv_text.strip().splitlines()), 3)
ok("CSV: кодировка с меткой для Excel", csv_path.read_bytes()[:3] == b"\xef\xbb\xbf")

# Бюджет: модель часто молчит, и тогда в таблицу должна идти цена объявления.
# Без этого «Общий бюджет» и «Средний чек» в таблице показывают нули.
st.upsert(conn, "Kwork", "Kwork-разработка", {"id": "b:1", "title": "Бот с ценой",
                                              "description": "бот", "url": "ub", "price": 21000,
                                              "published": NOW})
st.mark_notified(conn, "b:1", True)
st.save_llm(conn, "b:1", {"score": 9, "status": "ok", "summary": "s", "budget": ""})
row_b = [r for r in st.sheet_pending(conn, 7) if r["id"] == "b:1"][0]
check("пустой бюджет модели — берётся цена объявления",
      sheet_webhook.pick_budget(row_b["budget"], row_b["price"]), 21000)
st.save_llm(conn, "b:1", {"score": 9, "status": "ok", "summary": "s", "budget": "от 30 000"})
row_b = [r for r in st.sheet_pending(conn, 7) if r["id"] == "b:1"][0]
check("число из ответа модели важнее цены",
      sheet_webhook.pick_budget(row_b["budget"], row_b["price"]), 30000)
# Живой случай: модель написала текст без цифр, а в объявлении была реальная цена.
check("текст модели без цифр не перебивает цену объявления",
      sheet_webhook.pick_budget("не указан (просит предложить цену)", 15000), 15000)
check("нет ни цены, ни числа — пусто", sheet_webhook.pick_budget("", None), "")
st.mark_sheet_sent(conn, "b:1")

print("== без бюджета строка в таблицу не идёт ==")
st.upsert(conn, "FL.ru", "FL.ru", {"id": "n:1", "title": "Бот без цены", "description": "бот",
                                    "url": "un", "published": NOW})
st.save_llm(conn, "n:1", {"score": 9, "status": "ok", "summary": "s", "budget": ""})
check("нет ни цены, ни бюджета — в выборке таблицы нет",
      [r["id"] for r in st.sheet_pending(conn, 7) if r["id"] == "n:1"], [])
posted_n, _why_n = sheet_webhook.push_pending(
    conn, {"GOOGLE_SHEET_WEBHOOK_URL": "http://127.0.0.1:1/x", "SHEET_TOKEN": "t"}, 7)
check("и отправить её нечем — строк для таблицы нет", posted_n, 0)

st.upsert(conn, "Kwork", "Kwork-разработка", {"id": "n:2", "title": "Бот с суммой",
                                              "description": "бот", "url": "un2",
                                              "published": NOW})
st.save_llm(conn, "n:2", {"score": 9, "status": "ok", "summary": "s", "budget": "от 20 000"})
check("модель назвала сумму — строка в выборке есть",
      [r["id"] for r in st.sheet_pending(conn, 7) if r["id"] == "n:2"], ["n:2"])
st.mark_sheet_sent(conn, "n:2")

st.mark_sheet_sent(conn, "a:1")
check("после отправки в Таблицу строка не повторяется",
      [r["id"] for r in st.sheet_pending(conn, 3)], ["a:2"])

st.upsert(conn, "FL.ru", "FL.ru", {"id": "u:1", "title": "Бот без оценки", "description": "бот",
                                    "url": "u1", "published": NOW})
st.mark_notified(conn, "u:1", True)
check("неоценённый заказ виден добору", [r["id"] for r in st.unevaluated(conn)], ["u:1"])
st.save_llm(conn, "u:1", {"score": 5, "status": "ok"})
check("после оценки добор пуст", st.unevaluated(conn), [])

print("== P0: лимит карточек не обрывает источник ==")
import monitor as mon
mon.MAX_ITEMS_PER_RUN = 20
many = [{"id": f"x:{i}", "title": f"Нужен бот {i}", "description": "автоматизация",
         "url": f"u{i}", "published": NOW, "price": None} for i in range(60)]
mon.fetch_items = lambda source: (many, None)
mon.YIELD = TMP / "yield.json"
fresh, stats = mon.run_source({"name": "FL.ru", "type": "rss", "enabled": True},
                              {"keywords": ["бот"], "max_age_days": 7}, False, conn)
check("в карточки ушло 20", len(fresh), 20)
ok("в банк легли все 60",
   conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] >= 60)
check("остаток посчитан как capped", stats["capped"], 40)
check("ошибки нет", stats["error"], None)

print("== правило бюджета живёт в одном модуле ==")
import budget as money

check("пусто и нет цены — бюджета нет", money.has_budget("", None), False)
check("текст модели без цифр — бюджета нет", money.has_budget("не указан", None), False)
check("цена объявления считается бюджетом", money.has_budget("", 15000), True)
check("«5 000 ₽» → 5000", money.pick_budget("5 000 ₽", None), 5000)
ok("sheet_webhook берёт функцию оттуда же",
   sheet_webhook.pick_budget is money.pick_budget)
ok("и выборка таблицы тоже", "budget.has_budget" in (APP / "storage.py").read_text(encoding="utf-8"))
check("у чата и таблицы один ответ на «нет суммы»",
      (mon.is_lead({"status": "ok", "score": 9, "budget": ""}, {"price": None}, 7),
       money.has_budget("", None)), (False, False))

print("== стоп-кран: три отказа модели подряд ==")
check("порог стоп-крана — три отказа", mon.LLM_STOP_AFTER, 3)
check("два отказа — работаем дальше", mon.stop_cock(2), False)
check("три отказа — стоп", mon.stop_cock(3), True)
check("пять отказов — тоже стоп", mon.stop_cock(5), True)
_cycle_src = (APP / "monitor.py").read_text(encoding="utf-8")
ok("цикл спрашивает стоп-кран перед следующим вызовом", "if stop_cock(failures):" in _cycle_src)
ok("стоп-кран останавливает прогон", "break" in _cycle_src.split("if stop_cock(failures):")[1][:1200])
ok("успешный ответ обнуляет счётчик отказов", _cycle_src.count("failures = 0") >= 2)

print("== лид = порог И бюджет; в чат и в таблицу идёт один список ==")
check("оценка 9 с ценой — лид",
      mon.is_lead({"status": "ok", "score": 9, "budget": ""}, {"price": 15000}, 7), True)
check("оценка 9 без бюджета и без цены — не лид",
      mon.is_lead({"status": "ok", "score": 9, "budget": ""}, {"price": None}, 7), False)
check("модель назвала число — лид без цены объявления",
      mon.is_lead({"status": "ok", "score": 9, "budget": "от 30 000"}, {"price": None}, 7), True)
check("оценка ниже порога — не лид даже с ценой",
      mon.is_lead({"status": "ok", "score": 5, "budget": ""}, {"price": 9000}, 7), False)
check("сбой модели — не лид",
      mon.is_lead({"status": "error", "score": 9, "budget": ""}, {"price": 9000}, 7), False)
check("текст модели без цифр не спасает",
      mon.is_lead({"status": "ok", "score": 8, "budget": "не указан"}, {"price": None}, 7), False)

print("== доставка: упавшая отправка не теряет заказ ==")
conn.execute("DELETE FROM orders")
conn.execute("DELETE FROM llm_results")
conn.commit()
st.upsert(conn, "FL.ru", "FL.ru", {"id": "d:1", "title": "Бот", "description": "бот",
                                   "url": "u", "published": NOW})
st.mark_notified(conn, "d:1", True)
st.save_llm(conn, "d:1", {"score": 9, "status": "ok", "summary": "сделать"})
real_send = mon.tg_send
mon.tg_send = lambda text, env: False
card_item = {"title": "Бот", "description": "", "url": "u", "published": None, "source": "FL.ru"}
sent, failed = mon.deliver(conn, [("d:1", card_item, {"score": 9})], {})
check("отправка не прошла — так и записано", (sent, failed), (0, 1))
check("заказ остался в очереди", len(st.pending_delivery(conn, 7)), 1)

mon.tg_send = lambda text, env: True
sent, failed = mon.deliver(conn, [("d:1", card_item, {"score": 9})], {})
check("повторная отправка удалась", (sent, failed), (1, 0))
check("после успеха очередь пуста", len(st.pending_delivery(conn, 7)), 0)
mon.tg_send = real_send

print("== честность лога отправки и внешний признак жизни ==")
real_send = mon.tg_send
mon.tg_send = lambda text, env: False
check("мёртвый токен: дошло 0, не дошло 3", send_chunks(["a", "b", "c"], {}), (0, 3))
mon.tg_send = lambda text, env: True
check("живой токен: дошло 3, не дошло 0", send_chunks(["a", "b", "c"], {}), (3, 0))
mon.tg_send = real_send

_hb = TMP / "heartbeat.json"
real_hb = mon.HEARTBEAT
mon.HEARTBEAT = _hb
_t0 = time.time()
mon.heartbeat("partial", 1, 2)
data = json.loads(_hb.read_text(encoding="utf-8"))
check("признак жизни записан со статусом", data["status"], "partial")
check("в признаке видно, сколько не дошло", (data["sent"], data["failed"]), (1, 2))
ok("признак записан в этом прогоне", _hb.stat().st_mtime >= _t0 - 1)
check("холостой прогон не пишет признак", mon.heartbeat_status(True, 0), None)
check("настоящий цикл пишет «ok»", mon.heartbeat_status(False, 0), "ok")
check("частичный отказ — «partial»", mon.heartbeat_status(False, 2), "partial")
mon.HEARTBEAT = real_hb

print("== смена вёрстки не выглядит как «заказов нет» ==")


def raises(fn, *args):
    try:
        fn(*args)
    except Exception as exc:                    # noqa: BLE001
        return type(exc).__name__
    return None


check("kwork: маркера нет — кричит",
      raises(parse_kwork, '<html><script>{"otherData": [{"id": 1}]}</script></html>'),
      "StructureChanged")
check("kwork: маркер есть, массива нет — кричит",
      raises(parse_kwork, '<html><script>{"wantsListData": null}</script></html>'),
      "StructureChanged")
check("kwork: заказов правда нет — молчит",
      raises(parse_kwork, '<html><script>{"wantsListData": []}</script></html>'), None)
check("kwork: пустая лента — пустой список",
      parse_kwork('<html><script>{"wantsListData": []}</script></html>'), [])

real_yield = mon.YIELD
mon.YIELD = TMP / "yield2.json"
check("источник новый — тревоги нет", mon.yield_alarm({"name": "s1"}, 0), None)
check("источник дал 12 — тревоги нет", mon.yield_alarm({"name": "s1"}, 12), None)
ok("источник дал 0 после 12 — ТРЕВОГА", mon.yield_alarm({"name": "s1"}, 0))
check("dry-run не пишет историю",
      (mon.yield_alarm({"name": "s3"}, 7, remember=False), mon.yield_alarm({"name": "s3"}, 0)),
      (None, None))
mon.YIELD = real_yield

print("== RSS-разбор ==")
RSS = """<?xml version="1.0"?><rss><channel><item><title>Нужен парсер</title>
<link>https://www.fl.ru/projects/1</link><guid>1</guid>
<description>Собрать данные</description>
<pubDate>Sat, 15 Nov 2026 10:00:00 +0000</pubDate></item></channel></rss>""".encode("utf-8")
rss = parse_rss(RSS)
check("одна позиция разобрана", len(rss), 1)
check("дата из RFC-822", rss[0]["published"].strftime("%Y-%m-%d"), "2026-11-15")

print("== ядро админки: одна правда на две панели ==")
import admin_core as core

cfg_file = TMP / "config.json"
cfg_file.write_text(json.dumps({"keywords": ["бот"], "llm_min_score": 7,
                                "sources": [{"name": "FL.ru", "enabled": True}]}),
                    encoding="utf-8")
check("админка по умолчанию — кнопочная", core.get_ui(core.load(cfg_file)), "monitor")
check("переключение даёт веб-панель", core.switch_ui(cfg_file), "parser")
check("ключ записан в конфиг", json.loads(cfg_file.read_text())["admin_ui"], "parser")
check("переключение обратно", core.switch_ui(cfg_file), "monitor")
check("неизвестное значение не ломает выбор", core.get_ui({"admin_ui": "что-то"}), "monitor")
check("порог ставится", core.set_min_score(8, cfg_file), 8)
check("порог выше 10 обрезается", core.set_min_score(99, cfg_file), 10)
check("слово добавляется", core.add_word("Парсер", cfg_file), ["бот", "парсер"])
check("дубль слова не плодится", core.add_word("бот", cfg_file), ["бот", "парсер"])
check("слово удаляется", core.remove_word("бот", cfg_file), ["парсер"])
check("источник выключается", core.toggle_source("FL.ru", cfg_file), False)
check("неизвестный источник — None", core.toggle_source("Нет такого", cfg_file), None)
check("цена шагом ±1000", core.step_price(1000, cfg_file), 1000)

print("== расписание: когда цикл запускается сам ==")
check("по умолчанию выключено", core.read_schedule({"schedule": {}})["enabled"], False)
check("по умолчанию 6 часов", core.read_schedule({"schedule": {}})["interval_hours"], 6.0)
check("выключено — не пора",
      core.schedule_due({"schedule": {"enabled": False}}), (False, "расписание выключено — сбор только по кнопке"))
check("включено, ни разу не запускалось — пора",
      core.schedule_due({"schedule": {"enabled": True, "interval_hours": 6, "last_run": ""}})[0], True)
_now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
_recent = {"schedule": {"enabled": True, "interval_hours": 6,
                        "last_run": "2026-11-15T10:00:00+00:00"}}
check("прошло 2 из 6 часов — рано", core.schedule_due(_recent, _now)[0], False)
ok("причина названа словами", "рано" in core.schedule_due(_recent, _now)[1])
_old = {"schedule": {"enabled": True, "interval_hours": 6,
                     "last_run": "2026-11-14T09:00:00+00:00"}}
check("прошло больше 6 часов — пора", core.schedule_due(_old, _now)[0], True)
check("битая дата не блокирует сбор",
      core.schedule_due({"schedule": {"enabled": True, "interval_hours": 6, "last_run": "мусор"}}, _now)[0], True)
check("следующий запуск = прошлый + интервал",
      core.next_run_at(_recent, _now).strftime("%H:%M"), "16:00")
check("выключено — следующего нет", core.next_run_at({"schedule": {"enabled": False}}), None)

snap = core.snapshot(conn, core.load(cfg_file))
ok("снимок несёт счётчики", "orders" in snap["counters"])
check("снимок несёт вид админки", snap["admin_ui"], "monitor")

print("== кнопочная админка: порог и переключение ==")
import bot

sent = []
blocked, flaky = set(), set()


def fake_api(token, method, _sock=45, **params):
    if method == "deleteMessage" and params["message_id"] in blocked:
        raise urllib.error.HTTPError(
            "https://api.telegram.org/x", 400, "Bad Request", {},
            io.BytesIO(b'{"description":"Bad Request: message can\'t be deleted"}'))
    if method == "deleteMessage" and params["message_id"] in flaky:
        raise OSError("сеть моргнула")
    sent.append((method, params))
    if method == "sendMessage":
        return {"result": {"message_id": 1000 + len(sent)}}
    return {"result": True}


bot.api = fake_api
bot.PANEL_ID.clear()
bot.PENDING.clear()
BOT_CFG = {"keywords": ["бот", "парсер"], "exclude_words": [], "min_price": 2000,
           "show_vacancies": False, "admin_ui": "monitor", "llm_min_score": 7,
           "sources": [{"name": "FL.ru", "type": "api", "enabled": True}]}
bot.CONFIG = cfg_file
cfg_file.write_text(json.dumps(BOT_CFG, ensure_ascii=False), encoding="utf-8")
bot.PANEL_FILE = TMP / "panel.json"


def fake_load(path):
    if cfg_file.exists():
        return json.loads(cfg_file.read_text(encoding="utf-8"))
    return copy.deepcopy(BOT_CFG)


bot.load_config = fake_load


def last_kb():
    for method, params in reversed(sent):
        if method == "sendMessage":
            return json.loads(params["reply_markup"])
    return {}


BOT_SRC = Path(bot.__file__).read_text(encoding="utf-8")
check("панель на инлайн-кнопках (в чате, не у поля ввода)",
      '"inline_keyboard"' in BOT_SRC, True)
check("нажатия обрабатываются по callback_data", "callback_data" in BOT_SRC, True)

bot.show_home(1, 1)
ok("панель несёт инлайн-кнопки в сообщении", "inline_keyboard" in last_kb())
check("в панели 8 кнопок", sum(len(row) for row in last_kb()["inline_keyboard"]), 8)
_seen_sections = {label for row in bot.INLINE_MAIN for label, _code in row
                  if _code.startswith("s:")}
_seen_sections = {bot.SECTION_BY_CODE[code[2:]] for _l, code in
                  [(l, c) for row in bot.INLINE_MAIN for l, c in row] if code.startswith("s:")}
check("каждый раздел меню известен боту",
      all(label in bot.SECTIONS for label in _seen_sections), True)
ok("в панели есть кнопка проверки", any(c == "a:run" for row in bot.INLINE_MAIN for _l, c in row))

check("кнопка «Фильтры» открывает раздел", bot.handle_reply(1, 1, bot.SEC_FILTERS, 5), True)
_codes = [c for row in last_kb()["inline_keyboard"] for _t, c in
          [(b["text"], b["callback_data"]) for b in row]]
ok("в разделе «Фильтры» есть кнопки цены и порога",
   "a:price-up" in _codes and "a:s7" in _codes)
ok("в разделе есть кнопки порога",
   any("Порог 7" in label for row in bot.FILTERS_KB["keyboard"] for label in row))
check("кнопка «Слово» включает ввод текста", bot.handle_reply(1, 1, bot.WORD_ADD, 6), True)
check("ввод слова включён", bot.PENDING.get(1, {}).get("mode"), "add")
bot.PENDING.clear()

label8 = [lab for lab in bot.SCORE_LABELS if lab.endswith("8")][0]
check("кнопка порога нажимается", bot.handle_reply(1, 1, label8, 7), True)
check("порог ИИ записан в конфиг",
      json.loads(cfg_file.read_text(encoding="utf-8"))["llm_min_score"], 8)
check("кнопка переключения админки нажимается", bot.handle_reply(1, 1, bot.UI_LABEL, 8), True)
check("админка переключилась на веб-панель",
      json.loads(cfg_file.read_text(encoding="utf-8"))["admin_ui"], "parser")
check("цена выросла на 1000",
      bot.handle_reply(1, 1, bot.PRICE_UP, 9)
      and json.loads(cfg_file.read_text(encoding="utf-8"))["min_price"], 3000)
check("цена вернулась назад",
      bot.handle_reply(1, 1, bot.PRICE_DOWN, 10)
      and json.loads(cfg_file.read_text(encoding="utf-8"))["min_price"], 2000)
sent.clear()
bot.PENDING[1] = {"mode": "add", "prompt_id": None}
bot.prompt(1, 1, "Пришли слово")
_ask = json.loads([p for m, p in sent if m == "sendMessage"][-1]["reply_markup"])
check("при вводе слова подсказка несёт «Отмену»",
      _ask.get("inline_keyboard"), [[{"text": bot.CANCEL_LABEL, "callback_data": "a:cancel"}]])
ok("и не несёт клавиатуры", "keyboard" not in _ask)
bot.PENDING.clear()
bot.idle_deleter()

fired = []
bot.start_cycle = lambda token, chat: fired.append("cycle")
check("«Проверить сейчас» запускает цикл",
      bot.handle_reply(1, 1, bot.CHECK_LABEL, 13) and fired, ["cycle"])

print("== инлайн-нажатия: разделы, порог, переключение, промт ==")
bot.PANEL_ID.clear()
sent.clear()
bot.show_home(1, 1)
bot.handle_callback(1, 1, "s:filters", 500, "cb-1")
ok("переход в «Фильтры» правит то же сообщение",
   any(m == "editMessageText" for m, _p in sent))
_panel = json.loads([p for m, p in sent if m in ("editMessageText", "sendMessage")][-1]["reply_markup"])
ok("в фильтрах инлайн-кнопки фильтров",
   any(b["callback_data"] == "a:s7" for row in _panel["inline_keyboard"] for b in row))
bot.handle_callback(1, 1, "a:s8", 500, "cb-2")
check("кнопка «Порог 8» пишет конфиг", json.loads(cfg_file.read_text())["llm_min_score"], 8)
_cfg_now = json.loads(cfg_file.read_text())
_cfg_now["admin_ui"] = "monitor"
cfg_file.write_text(json.dumps(_cfg_now, ensure_ascii=False), encoding="utf-8")
bot.handle_callback(1, 1, "a:ui", 500, "cb-3")
check("кнопка «Сменить админку» переключает", json.loads(cfg_file.read_text())["admin_ui"], "parser")
bot.handle_callback(1, 1, "a:src:0", 500, "cb-4")
ok("кнопка источника выключает его",
   json.loads(cfg_file.read_text())["sources"][0]["enabled"] is False)
bot.handle_callback(1, 1, "a:srcy", 500, "cb-5")     # мусорный callback не должен падать
ok("неизвестное нажатие не роняет бота", True)

bot.handle_callback(1, 1, "s:prompt", 500, "cb-6")
_prompt_panel = json.loads([p for m, p in sent if m in ("editMessageText", "sendMessage")][-1]["reply_markup"])
ok("экран промта открывается", any(b["callback_data"] == "a:pr-edit"
                                   for row in _prompt_panel["inline_keyboard"] for b in row))
_text = [p.get("text", "") for m, p in sent if m in ("editMessageText", "sendMessage")][-1]
ok("экран промта показывает текст задания", "Промт для ИИ" in _text and "score" in _text)

bot.PENDING.clear()
bot.handle_callback(1, 1, "a:pr-edit", 500, "cb-7")
check("кнопка «Изменить» включает ввод промта", bot.PENDING.get(1, {}).get("mode"), "prompt")
bot.handle_text(1, 1, "Новый промт: {text}", 600)
check("присланный промт сохранён в конфиг",
      json.loads(cfg_file.read_text()).get("llm_prompt"), "Новый промт: {text}")
bot.PENDING.clear()
bot.handle_callback(1, 1, "a:pr-reset", 500, "cb-8")
check("кнопка «Стандартный» очищает промт",
      json.loads(cfg_file.read_text()).get("llm_prompt"), "")

print("== панель одна: кнопки под сообщением, клавиатуры нет ==")
bot.PENDING.clear()
bot.KEYBOARD_CLEARED.clear()
bot.PANEL_ID.clear()
sent.clear()
bot.show_home(1, 1)
_panel_kb = json.loads([p for m, p in sent if m == "sendMessage"][-1]["reply_markup"])
ok("панель — инлайн-кнопки", "inline_keyboard" in _panel_kb)
ok("клавиатуры у поля ввода нет", "keyboard" not in _panel_kb)
check("зависшую клавиатуру снимаем один раз за запуск",
      sum(1 for m, p in sent if m == "sendMessage"
          and "remove_keyboard" in (p.get("reply_markup") or "")), 1)

sent.clear()
bot.show_section(1, 1, "🔍 Фильтры")
_panel_kb = json.loads([p for m, p in sent if m == "sendMessage"][-1]["reply_markup"])
ok("в разделе тоже только инлайн", "keyboard" not in _panel_kb)
ok("повторно клавиатуру не снимаем (без мусора в ленте)",
   not any("remove_keyboard" in (p.get("reply_markup") or "") for _m, p in sent))

# Кнопка «🔀 Панель» осталась в старых сообщениях: переключать нечего, но нажатие
# не должно возвращать вторую панель.
sent.clear()
bot.handle_callback(1, 1, "a:panel", 500, "cb-p1")
_panel_kb = json.loads([p for m, p in sent if m in ("sendMessage", "editMessageText")][-1]["reply_markup"])
ok("после старой кнопки клавиатуры не появляется", "keyboard" not in _panel_kb)
ok("панель перерисована фильтрами",
   any(b["callback_data"] == "a:s7" for row in _panel_kb["inline_keyboard"] for b in row))
bot.idle_deleter()

# Главная проверка этой правки: ни одна панель не несёт reply-клавиатуру.
ok("show() без kb не ставит клавиатуру", "kb or MAIN_KB" not in BOT_SRC)
ok("в меню нет кнопки переключения панели",
   not any("Панель" == label for row in bot.MAIN_KB["keyboard"] for label in row))

print("== промт и отмена: только кнопки в сообщении ==")
bot.PENDING.clear()
_cfg_p = json.loads(cfg_file.read_text())
_cfg_p["llm_prompt"] = ""
cfg_file.write_text(json.dumps(_cfg_p, ensure_ascii=False), encoding="utf-8")
sent.clear()
bot.show_section(1, 1, "🤖 Промт")
_rows = json.loads([p for m, p in sent if m == "sendMessage"][-1]["reply_markup"])
ok("у «Промта» кнопки «Изменить» и «Стандартный»",
   {"a:pr-edit", "a:pr-reset"} <= {b["callback_data"] for row in _rows["inline_keyboard"]
                                   for b in row})
bot.PENDING[1] = {"mode": "add", "prompt_id": 555}
sent.clear()
bot.handle_callback(1, 1, "a:cancel", 500, "cb-cancel")
check("«Отмена» снимает ожидание ввода", bot.PENDING.get(1), None)
ok("после отмены — панель с кнопками, без клавиатуры",
   any("inline_keyboard" in (p.get("reply_markup") or "")
       for m, p in sent if m in ("sendMessage", "editMessageText")))
bot.idle_deleter()

print("== панель одна: прежнюю убираем ==")
bot.PANEL_ID.clear()
sent.clear()
bot.show_home(1, 1)
check("после отрисовки панель одна", len(bot.PANEL_ID[1]), 1)
_mark = len(sent)
bot.show_home(1, 1)
check("прежняя панель удалена", len(bot.PANEL_ID[1]), 1)
check("на перерисовку — один deleteMessage",
      sum(1 for m, _p in sent[_mark:] if m == "deleteMessage"), 1)
bot.idle_deleter()

print("== мои сообщения удаляются ==")
bot.PANEL_ID.clear()
sent.clear()
bot.show_home(1, 1)
bot.route("tok", 1, {"message": {"message_id": 4242, "chat": {"id": 1},
                                "text": "📋 Статус"}})
bot.idle_deleter()
ok("нажатие кнопки-клавиатуры удалено из чата",
   any(m == "deleteMessage" and p.get("message_id") == 4242 for m, p in sent))
sent.clear()
bot.route("tok", 1, {"message": {"message_id": 4343, "chat": {"id": 1}, "text": "просто текст"}})
bot.idle_deleter()
ok("любое сообщение владельца удаляется",
   any(m == "deleteMessage" and p.get("message_id") == 4343 for m, p in sent))

print("== быстрые нажатия схлопываются ==")


def press(mid, text):
    return {"update_id": mid, "message": {"message_id": mid, "text": text,
                                          "from": {"id": 1}, "chat": {"id": 1}}}


NAV = bot.SEC_FILTERS
check("навигация распознана", bot.is_nav_press(press(1, NAV), 1), True)
check("шаг цены не навигация", bot.is_nav_press(press(1, bot.PRICE_UP), 1), False)
check("чужой чат не трогаем", bot.is_nav_press(press(1, NAV), 99), False)
play, drop = bot.coalesce([press(i, NAV) for i in range(10, 15)], 1)
check("пять нажатий «Фильтры» → одно", len(play), 1)
check("играем последнее", play[0]["message"]["message_id"], 14)
check("пропущенные отданы на удаление", sorted(drop), [10, 11, 12, 13])
play, drop = bot.coalesce([press(20, bot.PRICE_UP), press(21, bot.PRICE_UP)], 1)
check("шаги цены не схлопываются", len(play), 2)

print("== веб-панель: страницы и действия ==")
import admin_web

core.CONFIG = cfg_file
admin_web.core.CONFIG = cfg_file
cfg_file.write_text(json.dumps(BOT_CFG, ensure_ascii=False), encoding="utf-8")


def test_connect():
    """Своё соединение на запрос: sqlite не любит чужие потоки."""
    new = st.sqlite3.connect(str(st.ORDERS_DB))
    new.executescript(st.SCHEMA)
    return new


admin_web.st.connect = test_connect
panel = admin_web.serve(0)
panel_port = panel.server_address[1]
threading.Thread(target=panel.serve_forever, daemon=True).start()


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{panel_port}{path}", timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, ""


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{panel_port}{path}", data=body,
                                     method="POST")
    with urllib.request.urlopen(request, timeout=5) as resp:
        return resp.status


status, body = get("/")
check("главная открывается", status, 200)
ok("на главной есть сводка", "Сводка" in body)
ok("на главной есть источники", "FL.ru" in body)
for path, marker in (("/sources", "Источники"), ("/words", "Ключевые слова"),
                     ("/score", "Порог ИИ"), ("/schedule", "Расписание"),
                     ("/export", "Экспорт"), ("/run", "Запуск"), ("/log", "Журнал")):
    code, page_body = get(path)
    check(f"экран «{marker}» открывается", code, 200)
    ok(f"экран «{marker}» подписан", marker in page_body)
check("неизвестный путь — 404, без падения", get("/no-such-page")[0], 404)

post("/set-score", {"score": "6"})
check("веб-панель меняет порог", json.loads(cfg_file.read_text())["llm_min_score"], 6)
post("/add-word", {"word": "Скрипт"})
ok("веб-панель добавляет слово", "скрипт" in json.loads(cfg_file.read_text())["keywords"])
post("/del-word", {"word": "скрипт"})
ok("веб-панель удаляет слово", "скрипт" not in json.loads(cfg_file.read_text())["keywords"])
post("/schedule-preset", {"preset": "6"})
_sched = json.loads(cfg_file.read_text())["schedule"]
check("пресет «каждые 6 ч» включён", _sched["enabled"], True)
check("интервал записан", _sched["interval_hours"], 6.0)
post("/schedule-preset", {"preset": "off"})
check("пресет «выключить» выключает", json.loads(cfg_file.read_text())["schedule"]["enabled"], False)
post("/toggle-source", {"name": "FL.ru"})
ok("веб-панель переключает источник",
   json.loads(cfg_file.read_text())["sources"][0]["enabled"] is False)
panel.shutdown()

print("== вебхук Таблицы: отказы не роняют цикл ==")
import sheet_webhook


class SheetStub(BaseHTTPRequestHandler):
    answer = b"ok"

    def log_message(self, *args):
        pass

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        SheetStub.last = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(SheetStub.answer)


sheet_server = ThreadingHTTPServer(("127.0.0.1", 0), SheetStub)
sheet_port = sheet_server.server_address[1]
threading.Thread(target=sheet_server.serve_forever, daemon=True).start()
sheet_url = f"http://127.0.0.1:{sheet_port}/exec"

check("без адреса работает только CSV",
      sheet_webhook.push_pending(conn, {}, 7),
      (0, "адрес вебхука не задан — работает только CSV"))
check("без токена не отправляет",
      sheet_webhook.push_pending(conn, {"GOOGLE_SHEET_WEBHOOK_URL": sheet_url}, 7)[0], 0)

# Строка для проверки транспорта: оценка 9 и цена в объявлении, чтобы она
# проходила правило «без бюджета не берём» и тест не зависел от прошлых строк.
st.upsert(conn, "Kwork", "Kwork-разработка", {"id": "s:1", "title": "Бот с ценой",
                                              "description": "бот", "url": "us1",
                                              "published": NOW, "price": 12000})
st.save_llm(conn, "s:1", {"score": 9, "status": "ok", "summary": "s"})
posted, why = sheet_webhook.push_pending(
    conn, {"GOOGLE_SHEET_WEBHOOK_URL": sheet_url, "SHEET_TOKEN": "t-1"}, 7)
check("строка ушла в Таблицу", (posted, why), (1, ""))
check("в теле есть токен", SheetStub.last.get("token"), "t-1")
ok("в теле есть оценка", SheetStub.last.get("score") == 9)
check("вторая попытка ничего не дублирует",
      sheet_webhook.push_pending(conn, {"GOOGLE_SHEET_WEBHOOK_URL": sheet_url,
                                        "SHEET_TOKEN": "t-1"}, 7), (0, ""))

st.upsert(conn, "FL.ru", "FL.ru", {"id": "a:9", "title": "Бот 9", "description": "бот",
                                    "url": "u9", "published": NOW, "price": 9000})
st.save_llm(conn, "a:9", {"score": 9, "status": "ok", "summary": "x"})
SheetStub.answer = b"deny"
posted, why = sheet_webhook.push_pending(
    conn, {"GOOGLE_SHEET_WEBHOOK_URL": sheet_url, "SHEET_TOKEN": "t-1"}, 7)
check("отказ токена не роняет цикл", posted, 0)
ok("причина отказа названа", "deny" in why)
SheetStub.answer = b"ok"
check("мёртвый адрес не роняет цикл",
      sheet_webhook.push_pending(conn, {"GOOGLE_SHEET_WEBHOOK_URL": "http://127.0.0.1:1/x",
                                        "SHEET_TOKEN": "t"}, 7)[0], 0)

print("== чужой скрипт: другая схема колонок и JSON-ответ ==")
# Таблица, сделанная другой заготовкой, отвечает JSON и читает другие имена полей.
# Это проверено по её коду: rating/link/status вместо score/url.
env_sheet = {"GOOGLE_SHEET_WEBHOOK_URL": sheet_url, "SHEET_TOKEN": "t-1"}
SheetStub.answer = b'{"ok":true,"fileUrl":""}'
posted, why = sheet_webhook.push_pending(conn, env_sheet, 7)
check("JSON-ответ «ok:true» считается приёмом", (posted, why), (1, ""))
check("в теле есть rating для чужой схемы", SheetStub.last.get("rating"), "5 - Высокая")
check("в теле есть link для чужой схемы", SheetStub.last.get("link"), "u9")
check("в теле есть status", SheetStub.last.get("status"), "Новый")
check("наша оценка тоже уходит", SheetStub.last.get("score"), 9)

st.upsert(conn, "FL.ru", "FL.ru", {"id": "a:10", "title": "Бот 10", "description": "бот",
                                    "price": 8000,
                                    "url": "u10", "published": NOW})
st.save_llm(conn, "a:10", {"score": 8, "status": "ok", "summary": "x"})
SheetStub.answer = b'{"ok":false,"error":"Invalid token"}'
posted, why = sheet_webhook.push_pending(conn, env_sheet, 7)
check("JSON-отказ не считается приёмом", posted, 0)
ok("причина отказа взята из ответа", "Invalid token" in why)

check("бюджет «5 000 ₽» уходит числом", sheet_webhook.budget_value("5 000 ₽"), 5000)
check("бюджет «от 15 000» → 15000", sheet_webhook.budget_value("от 15 000"), 15000)
check("бюджет без цифр → пусто", sheet_webhook.budget_value("по договорённости"), "")
check("пустой бюджет → пусто", sheet_webhook.budget_value(""), "")
check("чип 9 из 10 → «5 - Высокая»", sheet_webhook.rating_chip(9), "5 - Высокая")
check("чип 8 → «4 - Хорошая»", sheet_webhook.rating_chip(8), "4 - Хорошая")
check("чип 7 → «3 - Средняя»", sheet_webhook.rating_chip(7), "3 - Средняя")
check("чип ниже 6 → «1 - Спам»", sheet_webhook.rating_chip(2), "1 - Спам")
check("чип из мусора → «3 - Средняя»", sheet_webhook.rating_chip("нет числа"), "3 - Средняя")
check("текстовый ответ «ok» по-прежнему принимается",
      sheet_webhook.parse_answer("ok"), (True, ""))
check("JSON с ok:false объясняет отказ",
      sheet_webhook.parse_answer('{"ok":false,"error":"boom"}'), (False, "скрипт отказал: boom"))
check("HTML вместо ответа — понятная ошибка",
      sheet_webhook.parse_answer("<html>войдите</html>")[0], False)
sheet_server.shutdown()

print("== редирект 302 как у Apps Script ==")
# Живой Apps Script отвечает на POST кодом 302 на другой домен. Тело доходит
# на ПЕРВОМ запросе, а редирект нужен лишь чтобы забрать ответ. Переигрывать
# POST на адрес редиректа нельзя: Google отвечает 405, и строки не уходят —
# именно это случилось 15.11.2026 на настоящем развёртывании.
class RedirectStub(BaseHTTPRequestHandler):
    seen = {}

    def log_message(self, *args):
        pass

    def do_POST(self):                                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        RedirectStub.seen = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/echo")
        self.end_headers()

    def do_GET(self):                                    # noqa: N802
        if self.path != "/echo":
            self.send_response(404)
            self.end_headers()
            return
        payload = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


redir = ThreadingHTTPServer(("127.0.0.1", 0), RedirectStub)
redir_port = redir.server_address[1]
threading.Thread(target=redir.serve_forever, daemon=True).start()

st.mark_sheet_sent(conn, "a:10")          # убираем прошлую недосланную, чтобы счёт был ровный
st.upsert(conn, "FL.ru", "FL.ru", {"id": "a:11", "title": "Бот 11", "description": "бот",
                                    "price": 9000,
                                    "url": "u11", "published": NOW})
st.save_llm(conn, "a:11", {"score": 9, "status": "ok", "summary": "z"})
posted, why = sheet_webhook.push_pending(
    conn, {"GOOGLE_SHEET_WEBHOOK_URL": f"http://127.0.0.1:{redir_port}/exec",
           "SHEET_TOKEN": "t-1"}, 7)
check("редирект 302 не мешает отправке", (posted, why), (1, ""))
check("тело дошло на первом POST", RedirectStub.seen.get("token"), "t-1")
ok("в теле есть суть", RedirectStub.seen.get("summary") == "z")
check("после редиректа строка помечена отправленной",
      [r["id"] for r in st.sheet_pending(conn, 7)], [])
redir.shutdown()

print("== сеть: прокси для Telegram ==")
import net

check("прокси: берётся первый непустой", net.proxy_of({"A": "", "B": "http://x:1"}, "A", "B"), "http://x:1")
check("прокси: ключей нет — напрямую", net.proxy_of({}, "A", "B"), "")
check("прокси: разбор адреса", net.split_proxy("http://127.0.0.1:2082"), ("127.0.0.1", 2082))
check("прокси: порт по умолчанию", net.split_proxy("http://127.0.0.1"), ("127.0.0.1", 80))
try:
    net.split_proxy("socks5://127.0.0.1:2082")
    check("socks5 отвергается", "без ошибки", "ValueError")
except ValueError:
    check("socks5 отвергается", "ValueError", "ValueError")

seen = {}


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"{}"


real_opener = net.opener
net.opener = lambda proxy="": (seen.__setitem__("proxy", proxy),
                               type("O", (), {"open": lambda self, req, timeout=20: _Resp()})())[1]
result = mon.tg_send("текст", {"BOT_TOKEN": "t", "CHAT_ID": "1",
                               "TG_PROXY_URL": "http://127.0.0.1:2082"})
check("карточка идёт через прокси из .env", (result, seen.get("proxy")), (True, "http://127.0.0.1:2082"))
mon.tg_send("текст", {"BOT_TOKEN": "t", "CHAT_ID": "1", "LLM_PROXY_URL": "http://127.0.0.1:2083"})
check("если TG_PROXY_URL пуст — берётся LLM_PROXY_URL", seen.get("proxy"), "http://127.0.0.1:2083")
net.opener = real_opener

bot._TLS.proxy = "http://127.0.0.1:2082"
bot._TLS.conn = None
conn_proxy = bot._telegram_conn(5)
check("бот идёт туннелем к Telegram", conn_proxy._tunnel_host, "api.telegram.org")
check("бот подключается к прокси, а не к Telegram", conn_proxy.host, "127.0.0.1")
bot._TLS.proxy = ""
bot._TLS.conn = None
conn_direct = bot._telegram_conn(5)
check("без прокси — напрямую", conn_direct.host, "api.telegram.org")
bot._TLS.conn = None
bot._TLS.proxy = None

print("== зависимостей нет: только стандартная библиотека ==")
import ast as _ast
import sys as _sys

LOCAL = {p.stem for p in APP.glob("*.py")}
STDLIB = set(_sys.builtin_module_names) | {
    "__future__", "argparse", "ast", "base64", "collections", "concurrent", "copy",
    "csv", "datetime", "email",
    "fcntl", "html", "http", "io", "json", "os", "pathlib", "queue", "re", "shutil",
    "sqlite3", "subprocess", "sys", "tempfile", "threading", "time", "urllib", "xml",
}
imports = set()
for path in APP.glob("*.py"):
    for node in _ast.walk(_ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, _ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
extra = sorted(imports - STDLIB - LOCAL)
check("сторонних библиотек нет", extra, [])
check("нет python-telegram-bot", "telegram" in imports, False)
check("нет streamlit", "streamlit" in imports, False)
check("нет gspread", "gspread" in imports, False)

print("== публичность репозитория: секретов и мусора нет ==")
ROOT = APP.parent
# .env на рабочей машине есть — так и должно быть; в репозиторий он не попадает
# потому, что закрыт .gitignore. Проверяем правило, а не текущее состояние диска.
ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
for pattern in (".env", ".env.bak", "*.bak", "volume/", "exports/", "*.db"):
    ok(f".gitignore закрывает {pattern}", pattern in ignore)
# База на рабочей машине есть (это нормально) и лежит в volume/, который закрыт
# .gitignore. Проверяем, что она не вывалилась в корень или рядом с кодом.
ok("в корне и в app нет файлов .db",
   not list(ROOT.glob("*.db")) and not list(APP.glob("*.db")))
ok("нет внутренних документов штаба",
   not any((ROOT / name).exists() for name in ("STATUS.md", "ADAPT.md")))
ok("есть LICENSE", (ROOT / "LICENSE").exists())

print("== в файлах под гитом нет значений из .env ==")
# Сторож, которого не хватало 15.11.2026: 344 зелёных теста молчали, пока в
# docs/sheet-webhook.gs лежал настоящий SHEET_TOKEN. Проверяем факт, а не правило:
# значение из .env не должно встречаться ни в одном отслеживаемом файле.
import re as _re
import subprocess as _sp


def _env_value(name: str) -> str:
    path = APP / ".env"
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


try:
    _tracked = _sp.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True,
                       text=True, timeout=20).stdout.split()
except Exception:
    _tracked = []

if _tracked:
    for _key in ("SHEET_TOKEN", "BOT_TOKEN", "LLM_API_KEY"):
        _value = _env_value(_key)
        if len(_value) < 12:
            # Ключа нет (чистая копия без .env). Проверка всё равно нужна: иначе
            # в чистом репозитории проверок меньше, и число в README перестаёт
            # сходиться с фактом.
            check(f"{_key} не задан — в репозитории его быть не может", [], [])
            continue
        _leak = [f for f in _tracked
                 if _value in (ROOT / f).read_text(encoding="utf-8", errors="ignore")]
        # печатать значение нельзя даже в отчёте — поэтому сверяем пути файлов
        check(f"{_key} из .env не встречается в файлах под гитом", _leak, [])

_gs = (ROOT / "docs" / "sheet-webhook.gs").read_text(encoding="utf-8")
_line = next((l for l in _gs.splitlines() if "const SHEET_TOKEN" in l), "")
ok("в .gs стоит плейсхолдер, а не токен", "ВСТАВЬ" in _line or "ЗАМЕНИТЕ" in _line)
check("в .gs нет длинного значения вместо плейсхолдера",
      bool(_re.search(r"SHEET_TOKEN\s*=\s*'[A-Za-z0-9_\-]{20,}'", _gs)), False)
ok("есть .env.example", (ROOT / ".env.example").exists())
ok("есть docs/sheet-webhook.gs", (ROOT / "docs" / "sheet-webhook.gs").exists())

env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
for key in ("BOT_TOKEN", "CHAT_ID", "LLM_PROVIDER", "LLM_CLI", "LLM_MODEL", "LLM_PROXY_URL",
            "LLM_TIMEOUT", "LLM_PAUSE", "LLM_BASE_URL", "LLM_API_KEY",
            "GOOGLE_SHEET_WEBHOOK_URL", "SHEET_TOKEN"):
    ok(f"в .env.example объявлен {key}", key in env_example)
ok("в .env.example только плейсхолдеры", "ВСТАВЬ" in env_example)

gs = (ROOT / "docs" / "sheet-webhook.gs").read_text(encoding="utf-8")
check("в .gs токен объявлен первой строкой",
      gs.splitlines()[0].startswith("const SHEET_TOKEN"), True)
ok("в .gs есть doPost", "function doPost" in gs)
ok("в .gs сверяется токен", "SHEET_TOKEN" in gs.split("function doPost")[1])
# В веб-приложении нет открытого окна таблицы: «активный лист» может оказаться пустым,
# и тогда строка молча не запишется, хотя клиент получит «ok». Лист ищется по имени.
ok("в .gs лист ищется по имени", "getSheetByName" in gs)
ok("в .gs имя листа настраивается", "SHEET_NAME" in gs.split("function doPost")[0])
check("в .gs нет ненадёжного getActiveSheet()", "getActiveSheet()" in gs, False)
check("в .gs нет формы doGet (иначе токен утекает в браузер)", "function doGet" in gs, False)
ok("в .gs пишется статус", "data.status" in gs)
ok("в .gs оценка переводится в чип", "chip_(" in gs)
ok("в .gs бюджет приводится к числу", "budget_(" in gs)
ok("в .gs дата приводится к ГГГГ-ММ-ДД", "date_(" in gs)

print(f"\nИТОГ: {PASS}/{PASS + FAIL} — " + ("ALL_GREEN ✅" if not FAIL else "FAIL"))
raise SystemExit(1 if FAIL else 0)
