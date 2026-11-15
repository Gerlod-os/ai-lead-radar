#!/usr/bin/env python3
"""llm_filter.py — ИИ-квалификация заказа: два режима и ОДИН разбор ответа.

Зачем отдельный файл. Правила «что считать лидом» меняются чаще, чем сбор заказов.
Здесь только: собрать промпт → получить текст от модели → вытащить из текста JSON →
проверить поля. Как именно получен текст — дело режима:

  LLM_PROVIDER=opencode_cli   локальный CLI, бесплатная модель, 0 ₽.
                              Вызов списком аргументов (без shell), пустая временная
                              папка, запрет инструментов (deny-конфиг), HTTP-прокси.
  LLM_PROVIDER=openai_http    любой OpenAI-совместимый шлюз с ключом (ключ компании).
                              urllib, адрес и модель — только из .env.

Обе ветки возвращают ОДИН и тот же словарь, поэтому остальной код разницы не видит.

Что важно и почему (проверено 15.11.2026, см. ПЛАН-ai-lead-radar §1.3):
  * Клиент модели читает `AGENTS.md` из рабочей папки и исполняет чужие указания.
    Лечится так: рабочая папка — свежая пустая, аргументы — списком, а в окружении
    лежит deny-конфиг, который запрещает bash/edit/write/webfetch.
  * Модель иногда печатает баннер и рассуждения: строки вида «> build · model»
    и блок `<think>…</think>`. Их срезаем до поиска JSON.
  * Модель иногда оборачивает JSON в ```json. Берём всё от первой `{` до последней `}`.
  * Ни один сбой модели не роняет цикл: кривой JSON → статус invalid_json,
    таймаут/падение/код 401/429 → статус error. Оба статуса — повод повторить позже.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import categories
import urllib.error
import urllib.request

# Запрет инструментов для локального CLI. Это НЕ настройка качества ответа, а замок:
# без него модель в чужой папке может выполнить bash. Синтаксис V1 (1.18.x) —
# проверен живьём; V2-синтаксис `permissions: [...]` клиент 1.18 отвергает.
DENY_CONFIG = json.dumps({
    "$schema": "https://opencode.ai/config.json",
    "permission": {"bash": "deny", "edit": "deny", "write": "deny",
                   "webfetch": "deny", "websearch": "deny", "task": "deny"},
})

DEFAULT_TIMEOUT = 120          # секунд на один вызов модели
DEFAULT_PAUSE = 1.0            # пауза между вызовами: дешёвая страховка от лимита

STATUS_OK = "ok"
STATUS_INVALID = "invalid_json"   # ответ пришёл, но это не наш JSON
STATUS_ERROR = "error"            # ответа нет: таймаут, код, падение процесса

PROMPT_DEFAULT = (
    "Ты фильтр заказов на фриланс-биржах. Оцени, подходит ли заказ исполнителю, "
    "который делает автоматизацию, ботов, парсинг, сайты, скрипты и интеграции.\n"
    "Шкала 0–10: 9–10 — прямое совпадение с внятным описанием; 7–8 — близкая задача; "
    "4–6 — смежное или мало данных; 0–3 — не наше (рерайт, копипаст, подписчики в соцсетях, "
    "дизайн интерьера, вакансия в штат).\n"
    f"category — строго одно из восьми: {categories.PROMPT_LINE}. "
    "Других значений не придумывай: свободный текст в этой колонке ломает сводную таблицу.\n"
    "Ответь ТОЛЬКО JSON без пояснений и без markdown:\n"
    '{"score": 0, "category": "одно из восьми", "budget": "бюджет как в тексте или пусто",'
    ' "summary": "одна строка: что нужно сделать", "reason": "почему такая оценка"}\n\n'
    "Заказ:\n{text}"
)


def build_prompt(item: dict, cfg: dict) -> str:
    """Собрать промпт: шаблон из конфига (или встроенный) + текст заказа.

    Текст заказа обрезаем: длинные простыни не улучшают оценку, а стоят времени.
    """
    template = (cfg.get("llm_prompt") or PROMPT_DEFAULT)
    text = f"{item.get('title') or ''}\n{item.get('description') or ''}".strip()[:2000]
    if "{text}" in template:
        return template.replace("{text}", text)
    return f"{template}\n\nЗаказ:\n{text}"


def strip_noise(raw: str) -> str:
    """Срезать баннер CLI и рассуждения модели до чистого текста ответа."""
    text = raw or ""
    text = re.sub(r"<think>.*?</think>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^.*?>\s*build\s*·.*$", " ", text, count=1,
                  flags=re.MULTILINE)          # первая строка баннера «> build · <модель>»
    text = re.sub(r"```[a-zA-Z]*", " ", text)  # ```json / ```
    return text.strip()


def extract_json(raw: str) -> dict | None:
    """Достать объект JSON из ответа модели. None — если не вышло.

    Берём от первой `{` до последней `}`: так проходит и чистый JSON, и JSON в
    ```-блоке, и JSON с болтовнёй вокруг. Никаких «попробуем исправить кавычки» —
    кривой ответ честно считается кривым и уходит на повтор.
    """
    text = strip_noise(raw)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _to_score(value) -> int | None:
    """Оценка 0–10 целым. «7», 7, 7.0, «7/10» — все дают 7; мусор — None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        found = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
        if not found:
            return None
        number = float(found.group(0).replace(",", "."))
    return max(0, min(10, int(round(number))))


def normalize(data: dict | None, status: str = STATUS_OK) -> dict:
    """Привести ответ модели к одному словарю с известными полями.

    Оценки нет — это не «ok»: без score карточку не к чему привязать, поэтому статус
    становится invalid_json (его переспросят), а не тихий ноль.
    """
    result = {"is_lead": False, "category": "", "budget": "", "summary": "",
              "score": 0, "reason": "", "status": status}
    if not isinstance(data, dict):
        result["status"] = STATUS_INVALID
        return result
    score = _to_score(data.get("score"))
    if score is None:
        result["status"] = STATUS_INVALID
        result["reason"] = str(data.get("reason") or "")[:300]
        return result
    result["score"] = score
    # Категория — всегда канон из восьми: свободный текст модели ломает
    # выпадающие списки и сводные в таблице (в архиве было 26 разных значений).
    result["category"] = categories.normalize(data.get("category"))
    result["budget"] = str(data.get("budget") or "")[:60]
    result["summary"] = str(data.get("summary") or "")[:300]
    result["reason"] = str(data.get("reason") or "")[:300]
    result["is_lead"] = bool(data.get("is_lead")) if "is_lead" in data else score >= 7
    return result


# ---------------------------------------------------------------- режим 1: CLI

KNOWN_ERRORS = (
    # Порядок важен: сначала частные случаи, потом общие коды.
    ("free usage limit reached",
     "исчерпан бесплатный лимит моделей OpenCode — смени модель или подключи свой ключ"),
    ("free models work in opencode",
     "бесплатные модели OpenCode работают только внутри их клиента — нужен свой ключ"),
    ("no longer supports free models",
     "эта версия OpenCode больше не поддерживает бесплатные модели (v1) — нужен v2 или ключ"),
    ("only be used from within opencode",
     "бесплатные модели OpenCode доступны только внутри их клиента — нужен свой ключ"),
    ("permission", "модель отвергла запрос из-за запрета инструментов (permission)"),
    ("unauthorized", "ключ не принят (401) — проверь LLM_API_KEY"),
    ("429", "слишком много запросов (429) — подожди или смени провайдера"),
)


def explain_error(raw: str) -> str:
    """Перевести ответ клиента в человеческую причину.

    Зачем: 15.11.2026 вызов падал «таймаутом 120 с», хотя в отладочном логе стояло
    «Free usage limit reached». Клиент при этом не завершается — он висит, и без
    разбора причины каждый цикл тратил бы по две минуты на заведомо мёртвый вызов.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    low = text.lower()
    for marker, message in KNOWN_ERRORS:
        if marker in low:
            return message
    return text[:300]


def call_cli(prompt: str, env: dict, cli: str, model: str,
             proxy: str = "", timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """Спросить локальный CLI. Вернуть (текст ответа, текст ошибки). Одно из двух пусто.

    Рабочая папка — свежий временный каталог: так модель не видит ни файлов проекта,
    ни `AGENTS.md` штаба. Папка удаляется в finally, даже если процесс упал.

    `--print-logs` печатает причину отказа в stderr. Без него клиент на исчерпанном
    лимите молча висит, и мы видим только «таймаут», хотя причина известна сразу.
    Таймаут ловим сами (Popen + communicate), чтобы забрать уже напечатанное.
    """
    args = [cli, "run", "--dir", "", "--model", model, "--print-logs", "--log-level", "ERROR", prompt]
    workdir = tempfile.mkdtemp(prefix="lead-radar-llm-")
    args[3] = workdir
    child_env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        child_env.pop(key, None)          # чужое окружение не должно подменять прокси
    if proxy:
        # Только HTTP-прокси: HTTP-стек клиента не умеет socks5 (проверено 15.11.2026).
        child_env["HTTP_PROXY"] = child_env["HTTPS_PROXY"] = proxy
        child_env["http_proxy"] = child_env["https_proxy"] = proxy
        child_env["NO_PROXY"] = child_env["no_proxy"] = "localhost,127.0.0.1"
    # Запрет инструментов. Важно: `read` здесь НЕ запрещён намеренно. Сплошной
    # `read: deny` у бесплатных моделей OpenCode вызывает ответ «403 free tier»
    # независимо от квоты (известная ошибка клиента, ноябрь 2026). Нам он не нужен:
    # рабочая папка пустая, читать в ней нечего.
    child_env["OPENCODE_CONFIG_CONTENT"] = DENY_CONFIG
    try:
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=child_env, cwd=workdir)
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            combined = (err or "") + (out or "")
            reason = explain_error(combined)
            # Разбор причины важнее факта таймаута: «исчерпан лимит» говорит, что
            # менять, а «таймаут 120 с» — только что модель молчит.
            if reason and reason != combined[:300].strip():
                return "", f"клиент не ответил за {timeout} с: {reason}"
            return "", f"таймаут {timeout} с"
        if process.returncode == 0:
            return out, ""
        combined = (err or "") + (out or "")
        low = combined.lower()
        if "unknown option" in low or "unknown argument" in low or "invalid option" in low:
            # Другая версия клиента не знает флагов логирования. Повторяем без них:
            # потеряем текст причины, но ответ получим. Папка ещё жива — повтор идёт
            # до finally, иначе cwd уже удалён.
            plain = [a for a in args if a not in ("--print-logs", "ERROR", "--log-level")]
            try:
                again = subprocess.run(plain, capture_output=True, text=True,
                                       timeout=timeout, env=child_env, cwd=workdir)
            except subprocess.TimeoutExpired:
                return "", f"таймаут {timeout} с"
            if again.returncode == 0:
                return again.stdout, ""
            return "", explain_error((again.stderr or "") + (again.stdout or "")) \
                or f"код {again.returncode}"
        return "", explain_error(combined) or f"код {process.returncode}"
    except OSError as error:
        return "", f"не смог запустить {cli}: {error}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ------------------------------------------------- режим 2: OpenAI-совместимый HTTP

def call_http(prompt: str, env: dict, base_url: str, api_key: str, model: str,
              timeout: int = DEFAULT_TIMEOUT) -> tuple[str, str]:
    """Спросить шлюз по протоколу OpenAI. Вернуть (текст ответа, текст ошибки).

    Один POST на `<base>/chat/completions`. Коды 401/429/5xx и таймаут — это ошибка
    вызова, а не приговор: наверх уходит текст, статус выставит normalize().
    """
    if not base_url or not model:
        return "", "не задан LLM_BASE_URL или LLM_MODEL"
    if api_key and not api_key.isascii():
        # Заголовки HTTP — только ASCII. Ключ с кириллицей (или с невидимым символом
        # из копипаста) иначе упадёт UnicodeEncodeError внутри urllib, и цикл
        # оборвётся на первом же заказе. Лучше сказать это словами.
        return "", "LLM_API_KEY содержит не-ASCII символы — проверь копипаст"
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = ""
        try:
            detail = error.read()[:200].decode("utf-8", "ignore")
        except Exception:
            pass
        return "", f"HTTP {error.code} {detail}".strip()
    except urllib.error.URLError as error:
        return "", f"сеть: {error.reason}"
    except (TimeoutError, OSError) as error:
        return "", f"таймаут/сеть: {error}"
    try:
        data = json.loads(payload)
        return data["choices"][0]["message"]["content"] or "", ""
    except (ValueError, KeyError, IndexError, TypeError):
        return "", "ответ шлюза не похож на OpenAI (нет choices[0].message.content)"


# --------------------------------------------------------------- общий вход

def settings(env: dict) -> dict:
    """Все настройки ИИ из окружения — в одном месте. В коде нет ни адреса, ни модели."""
    return {
        "provider": (env.get("LLM_PROVIDER") or "opencode_cli").strip().lower(),
        "cli": env.get("LLM_CLI") or "opencode",
        "model": env.get("LLM_MODEL") or "",
        "proxy": env.get("LLM_PROXY_URL") or "",
        "base_url": env.get("LLM_BASE_URL") or "",
        "api_key": env.get("LLM_API_KEY") or "",
        "timeout": int(env.get("LLM_TIMEOUT") or DEFAULT_TIMEOUT),
        "pause": float(env.get("LLM_PAUSE") or DEFAULT_PAUSE),
    }


def ask(prompt: str, env: dict, conf: dict | None = None) -> tuple[str, str, str]:
    """Спросить модель выбранным режимом. Вернуть (текст, ошибка, имя режима)."""
    conf = conf or settings(env)
    if conf["provider"] == "openai_http":
        text, error = call_http(prompt, env, conf["base_url"], conf["api_key"],
                                conf["model"], conf["timeout"])
        return text, error, "openai_http"
    text, error = call_cli(prompt, env, conf["cli"], conf["model"],
                           conf["proxy"], conf["timeout"])
    return text, error, "opencode_cli"


def qualify(item: dict, cfg: dict, env: dict, conf: dict | None = None,
            sleep=time.sleep) -> dict:
    """Оценить один заказ. Попытка одна + один повтор — и всегда словарь с полями.

    Три исхода: ok (есть score), invalid_json (ответ есть, но не наш), error
    (ответа нет). Повторяем ЛЮБОЙ неудачный исход один раз: и кривой JSON, и
    таймаут лечатся повтором чаще, чем кажется.
    """
    conf = conf or settings(env)
    prompt = build_prompt(item, cfg)
    result = normalize(None, STATUS_ERROR)
    for attempt in (1, 2):
        text, error, _mode = ask(prompt, env, conf)
        if error:
            result = {"is_lead": False, "category": "", "budget": "", "summary": "",
                      "score": 0, "reason": error[:300], "status": STATUS_ERROR}
        else:
            result = normalize(extract_json(text))
        result["attempts"] = attempt
        if result["status"] == STATUS_OK:
            return result
        if attempt == 1:
            sleep(conf.get("pause", DEFAULT_PAUSE) or DEFAULT_PAUSE)
    return result


def main() -> int:
    """Ручная проверка режима без базы: python3 llm_filter.py "текст заказа"."""
    import sys

    from sources import load_env
    from pathlib import Path

    env = load_env(str(Path(__file__).resolve().parent / ".env"))
    conf = settings(env)
    if not conf["model"]:
        print("в .env нет LLM_MODEL — режим не настроен")
        return 2
    data = qualify({"title": sys.argv[1] if len(sys.argv) > 1 else "тест", "description": ""},
                   {}, env, conf)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"режим: {conf['provider']}")
    return 0 if data["status"] == STATUS_OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
