const SHEET_TOKEN = 'ВСТАВЬ_СВОЙ_ТОКЕН';   // ← 20+ символов, латиница и цифры
const SHEET_NAME = 'Лист1';              // ← имя листа внизу таблицы, буква в букву

// docs/sheet-webhook.gs — приёмник строк для Google Таблицы (Apps Script).
// Подробная инструкция: docs/TABLITSA.md
//
// Колонки таблицы (шапка в 4-й строке, данные с 5-й):
//   A Дата · B Источник · C Оценка · D Категория · E Бюджет ·
//   F Суть · G Ссылка · H Файл · I Статус
// H остаётся пустой намеренно: радар не присылает файлов. I = «Новый».
//
// Формы (doGet) здесь НЕТ и не должно быть: в большинстве заготовок она вставляет
// токен прямо в HTML страницы — и любой, кто откроет адрес, прочитает его в исходнике
// и сможет писать в таблицу вашим именем. Радару форма не нужна: он только дописывает
// строки POST-запросом.
//
// Развёртывание: Deploy → Manage deployments → карандаш → Version: New version
//   Execute as: Me (USER_DEPLOYING)
//   Who has access: Anyone, even anonymous (ANYONE_ANONYMOUS)
// Просто «Anyone» по документации значит «любой вошедший» — POST из Python без
// логина упрётся в страницу авторизации.

/** Ответ клиенту. Короткий текст: клиент понимает и его, и JSON. */
function answer_(text) {
  return ContentService.createTextOutput(text).setMimeType(ContentService.MimeType.TEXT);
}

/** Дата в виде ГГГГ-ММ-ДД: так её понимает формат колонки A. */
function date_(raw) {
  var text = String(raw || '').trim();
  var found = text.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (found) return found[1] + '-' + found[2] + '-' + found[3];
  var zone = Session.getScriptTimeZone() || 'GMT+3';
  return Utilities.formatDate(new Date(), zone, 'yyyy-MM-dd');
}

/** Оценка 0–10 → подпись чипа 1–5. Уже подпись («5 - Высокая») возвращается как есть. */
function chip_(value) {
  var number = Number(value);
  if (isNaN(number)) return String(value || '3 - Средняя');
  if (number >= 9) return '5 - Высокая';
  if (number >= 8) return '4 - Хорошая';
  if (number >= 7) return '3 - Средняя';
  if (number >= 6) return '2 - Низкая';
  return '1 - Спам';
}

/** Бюджет числом. Пусто или без цифр — пустая ячейка, а не ноль. */
function budget_(value) {
  if (value === '' || value === null || value === undefined) return '';
  var number = Number(String(value).replace(/\s/g, ''));
  return isNaN(number) ? '' : number;
}

/**
 * Принять строку и дописать её в лист SHEET_NAME.
 *
 * Порядок проверок важен: сначала блокировка (две одновременные записи не должны
 * наложиться друг на друга), потом разбор, потом токен. Лист ищется по имени:
 * в веб-приложении нет открытого окна таблицы, поэтому «активный лист» может
 * оказаться пустым — и строка молча не запишется, хотя клиент получит «ok».
 */
function doPost(e) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return answer_('busy');
  try {
    var data;
    try {
      data = JSON.parse((e.postData && e.postData.contents) || '{}');
    } catch (err) {
      return answer_('bad json');
    }
    if (!SHEET_TOKEN || data.token !== SHEET_TOKEN) return answer_('deny');

    var book = SpreadsheetApp.getActiveSpreadsheet();
    if (!book) return answer_('no spreadsheet');
    var sheet = book.getSheetByName(SHEET_NAME) || book.getSheets()[0];
    if (!sheet) return answer_('no sheet');

    var score = (data.score !== undefined && data.score !== null) ? data.score : data.rating;
    sheet.appendRow([
      date_(data.date),                 // A Дата
      data.source || 'Веб-хук',         // B Источник
      chip_(score),                     // C Оценка (чип 1–5)
      data.category || 'Другое',        // D Категория
      budget_(data.budget),             // E Бюджет (число)
      data.summary || data.text || '',  // F Суть
      data.link || data.url || '',      // G Ссылка
      '',                               // H Файл — радар файлов не присылает
      data.status || 'Новый'            // I Статус
    ]);
    return answer_('ok');
  } catch (err) {
    return answer_('error: ' + err);
  } finally {
    try { lock.releaseLock(); } catch (err) {}
  }
}

/**
 * Проверка вручную: запусти из редактора — в журнале будет «pong» и имя листа,
 * в который пишет вебхук. Отличает «скрипт не развёрнут» от «лист не тот».
 */
function testPing() {
  var book = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = book ? (book.getSheetByName(SHEET_NAME) || book.getSheets()[0]) : null;
  Logger.log('pong; лист: ' + (sheet ? sheet.getName() : 'НЕ НАЙДЕН'));
}
