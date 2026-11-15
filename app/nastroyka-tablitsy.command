#!/bin/bash
# app/nastroyka-tablitsy.command — настройка Google Таблицы в один двойной щелчок.
#
# Что делает: спрашивает адрес веб-приложения и токен, проверяет адрес на опечатки,
# записывает оба значения в app/.env (не затирая остальные настройки) и проверяет
# трубу — до того, как вы запустите цикл.
#
# Чего НЕ делает: не ходит в ваш Google-аккаунт и не вставляет скрипт в таблицу —
# это делается руками в браузере, потому что требует вашего логина.
#
# Проверка трубы нарочно отправляет запрос с НЕВЕРНЫМ токеном: скрипт обязан ответить
# «deny». Это доказывает три вещи сразу — адрес живой, скрипт развёрнут, доступ открыт
# анонимным запросам. И ни одной строки в таблицу при этом не добавляется.

set -u
cd "$(dirname "$0")" || exit 1

echo
echo "=== Настройка Google Таблицы для ai-lead-radar ==="
echo
echo "Нужны два значения из развёрнутого скрипта:"
echo "  1) адрес веб-приложения — длинный, заканчивается на /exec"
echo "  2) токен — то, что стоит в первой строке скрипта (SHEET_TOKEN)"
echo

read -r -p "Адрес (вставьте и нажмите Enter): " URL
read -r -s -p "Токен (ввод не отображается, это нормально): " TOKEN
echo

URL="$(printf '%s' "$URL" | tr -d '[:space:]')"
TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"

if [ -z "$URL" ] || [ -z "$TOKEN" ]; then
  echo "✗ Пустой адрес или токен. Ничего не менял."
  echo "  Нажмите Enter, чтобы закрыть."
  read -r _
  exit 1
fi

if [ "${URL%/exec}" = "$URL" ]; then
  echo "✗ Адрес не заканчивается на /exec."
  echo "  Похоже, скопирован адрес /dev (он работает только под вашим логином)"
  echo "  или строка обрезалась. Скопируйте адрес кнопкой «Копировать» в окне развёртывания."
  echo "  Нажмите Enter, чтобы закрыть."
  read -r _
  exit 1
fi

if [ ${#TOKEN} -lt 16 ]; then
  echo "! Токен короче 16 символов — это не ошибка запуска, но слабый токен."
  echo "  Продолжаю: главное, чтобы он совпадал с первой строкой скрипта."
fi

ENV=".env"
touch "$ENV"
cp "$ENV" "$ENV.bak"

# Убираем прежние значения этих двух ключей, остальные строки не трогаем.
TMP="$(mktemp)"
grep -v -E '^(GOOGLE_SHEET_WEBHOOK_URL|SHEET_TOKEN)=' "$ENV" > "$TMP" || true
{
  cat "$TMP"
  printf 'GOOGLE_SHEET_WEBHOOK_URL=%s\n' "$URL"
  printf 'SHEET_TOKEN=%s\n' "$TOKEN"
} > "$ENV"
rm -f "$TMP"

echo "✓ Записано в app/.env (прежняя версия — app/.env.bak)"
echo

echo "Проверяю трубу (запрос с намеренно неверным токеном, в таблицу ничего не пишется)..."
PROBE="$(/usr/bin/python3 - "$URL" <<'PY'
import json, sys, urllib.error, urllib.request

url = sys.argv[1]
body = json.dumps({"token": "probe-нарочно-неверный", "score": 1}).encode()
request = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        answer = response.read().decode("utf-8", "ignore").strip()
        print("code", response.status)
        print("body", answer[:200])
except urllib.error.HTTPError as error:
    print("code", error.code)
    print("body", error.read()[:200].decode("utf-8", "ignore"))
except Exception as error:                     # noqa: BLE001
    print("code 0")
    print("body", str(error)[:200])
PY
)"

CODE="$(printf '%s\n' "$PROBE" | sed -n 's/^code //p')"
BODY="$(printf '%s\n' "$PROBE" | sed -n 's/^body //p')"

case "$CODE" in
  200)
    case "$BODY" in
      *deny*)
        echo "✓ Пайп живой: скрипт ответил «deny» — значит, развёрнут и доступ открыт."
        echo "  Значит, дальше только вопрос совпадения токена."
        ;;
      *ok*)
        echo "! Скрипт ответил «ok» на неверный токен — проверка токена в нём отключена."
        echo "  Это опасно: писать в таблицу сможет любой, кто узнал адрес."
        ;;
      *"bad json"*)
        echo "! Скрипт ответил «bad json» — похоже, в нём нет разбора JSON-тела."
        echo "  Проверьте, что вставлен наш скрипт, а не другой."
        ;;
      *"<html"*|*"<!DOCTYPE"*|*"Страница"*)
        echo "✗ Вернулась HTML-страница: скорее всего, доступ стоит не «Все»."
        echo "  Развернуть → Управление развёртываниями → карандаш →"
        echo "  У кого есть доступ: Все (не «Все, у кого есть аккаунт Google») → Новая версия."
        ;;
      *)
        echo "? Ответ не распознан, вот он целиком:"
        echo "  $BODY"
        ;;
    esac
    ;;
  401|403)
    echo "✗ HTTP $CODE — доступ закрыт. Нужно «Все» (Any user, even anonymous),"
    echo "  а не «Все, у кого есть аккаунт Google»."
    ;;
  404)
    echo "✗ HTTP 404 — адрес битый или это адрес /dev вместо /exec."
    ;;
  0)
    echo "✗ Не дозвонился: $BODY"
    echo "  Проверьте интернет и то, что адрес скопирован целиком."
    ;;
  *)
    echo "? Неожиданный код ответа: $CODE"
    echo "  $BODY"
    ;;
esac

echo
echo "Дальше:"
echo "  cd app && python3 monitor.py     # цикл добавит лиды в таблицу"
echo "  Если в логе «скрипт отверг токен (deny)» — токен в .env и в скрипте различаются."
echo
echo "Нажмите Enter, чтобы закрыть."
read -r _
