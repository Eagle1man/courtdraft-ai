/* Логика интерфейса помощника судьи.
 *
 * Порядок работы повторяет путь судьи: выбрать дело → увидеть материалы →
 * сформировать проект → прочитать его по разделам → посмотреть, что
 * требует проверки → при необходимости внести правку. Те же шаги
 * показаны полосой сверху, чтобы пользователь всегда видел, где он.
 *
 * Язык интерфейса: аудитория — секретарь и судья, люди без IT.
 * Поэтому коды данных (DRAFT_READY, REVIEW_REQUIRED, 413) никогда
 * не показываются: статусы названы словами, ошибки — это действие
 * («файл больше 50 МБ»), а не номер ответа.
 *
 * Формирование идёт в фоне и занимает минуты, поэтому здесь есть опрос
 * состояния задачи. Пока опрос идёт, кнопки блокируются: иначе пользователь
 * запустит вторую генерацию поверх первой и получит две расходящиеся версии.
 */

// База API. Обычно интерфейс раздаётся тем же бэкендом и хватает пустой
// строки. Но при публикации туннелем вида https://host/courtdraft/app/
// запросы обязаны идти в /courtdraft/api/... — иначе они уйдут в корень
// чужого сайта, который живёт на том же домене.
const API = (() => {
  const marker = "/app/";
  const at = window.location.pathname.indexOf(marker);
  return at > 0 ? window.location.pathname.slice(0, at) : "";
})();
const POLL_INTERVAL = 3000;
// Версия сборки интерфейса. Должна совпадать с ?v= в index.html:
// браузер кэширует app.js по этому адресу, и при неизменном ?v он
// продолжает показывать старый файл, даже когда на сервере новый.
const UI_BUILD = "0.8.4";

// Код участника живёт в памяти вкладки (sessionStorage), а не в браузере
// навсегда. Компьютер в аудитории общий: перезагрузка страницы код
// сохраняет, а закрытая вкладка или окно — забывает, и следующий участник
// вводит свой. Раньше код лежал в localStorage, и на том же компьютере
// при следующем заходе открывалась чужая сессия.
// Код участника больше не запрашивается при запуске: программа грузится
// сразу. Код нужен только для разделения дел и хранится в sessionStorage.
const STUDENT_KEY = "courtdraft_student_code";
const DEFAULT_STUDENT = "DEMO";

function currentStudentCode() {
  try {
    return (sessionStorage.getItem(STUDENT_KEY) || DEFAULT_STUDENT).trim();
  } catch {
    return DEFAULT_STUDENT;
  }
}

function rememberStudentCode(code) {
  try {
    if (code) sessionStorage.setItem(STUDENT_KEY, code);
  } catch {
    // Приватный режим браузера может запрещать хранилище — не повод
    // ломать создание дела.
  }
}

/* Выйти из сессии участника: забыть код и убрать его данные с экрана.

   Нужно именно действие, а не закрытие браузера: компьютер общий, и
   следующий человек не должен увидеть дела предыдущего. */
function forgetStudentCode() {
  try {
    sessionStorage.removeItem(STUDENT_KEY);
    // Старое постоянное значение: если оно осталось от прежней версии,
    // больше не используется, но и лежать ему незачем.
    localStorage.removeItem(STUDENT_KEY);
  } catch {
    // Доступ к хранилищу запрещён — код всё равно уйдёт из памяти вкладки.
  }
}

/* Примешивает код участника к адресу запроса.

   Изоляция держится на том, что сервер фильтрует по коду, а клиент его
   присылает. Без кода сервер отдаёт всё — это нужно администратору на
   разборе, поэтому пустое значение не подставляется. */
function withStudentCode(path) {
  const code = currentStudentCode();
  if (!code) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}student_code=${encodeURIComponent(code)}`;
}

/* Код — не украшение запроса, а ключ к своим делам.
   Сервер без кода отдаёт всё (это режим администратора), а дело,
   созданное без кода, видит каждый участник. Поэтому интерфейс без
   кода не показывает список и не создаёт дела, а спрашивает код. */
function studentGateOpen() {
  $("gate-student").value = currentStudentCode();
  $("gate-error").hidden = true;
  // Кнопка выхода нужна только тем, кто уже вошёл: незачем предлагать
  // «выйти» человеку, который ещё не вводил код.
  $("gate-forget").hidden = !currentStudentCode();
  $("student-gate").hidden = false;
  $("gate-student").focus();
}

/* Выйти из сессии: код забыт, дела прежнего участника убраны с экрана. */
function forgetStudent() {
  forgetStudentCode();
  state.cases = [];
  state.currentCase = null;
  state.documents = [];
  state.draft = null;
  state.loadingCaseId = null;
  $("doc-list").innerHTML = "";
  $("case-list").innerHTML = "";
  $("gate-student").value = "";
  renderStudentBadge();
  renderSteps();
  updateDocsLabel();
  studentGateOpen();
  $("gate-student").focus();
}

function renderStudentBadge() {
  const code = currentStudentCode();
  $("student-badge").textContent = code || "не выбран";
  $("btn-student").title = code
    ? `Текущий код: ${code}. Нажмите, чтобы сменить (например, перед работой следующего участника).`
    : "Введите код участника";
}

async function acceptStudentCode() {
  const code = $("gate-student").value.trim();
  if (!code) {
    $("gate-error").hidden = false;
    $("gate-student").focus();
    return;
  }
  const previous = currentStudentCode();
  rememberStudentCode(code);
  renderStudentBadge();
  $("student-gate").hidden = true;

  // Код сменился — данные прежнего участника не остаются на экране.
  if (previous && previous !== code) {
    state.currentCase = null;
    state.loadingCaseId = null;
    state.documents = [];
    state.draft = null;
    $("doc-list").innerHTML = "";
    updateDocsLabel();
    renderSteps();
  }

  await loadCases();
  if (state.cases.length) await selectCase(state.cases[0].id);
}

const state = {
  cases: [],
  currentCase: null,
  documents: [],
  draft: null,
  jobTimer: null,
  factsTimer: null,
  features: {},
  loadingCaseId: null,
  loadSeq: 0,
  loadRetries: 0,
  // Вид документа: "judgment" (по умолчанию) или "claim" (иск, истец).
  docKind: "judgment",
  // Введённые данные иска по делам: при переключении дела форма не теряется.
  claimData: {},
  // Метаданные автозаполнения иска из материалов (filled/missing/message).
  claimSuggest: {},
  // Ведётся ли запрос автозаполнения (чтобы не дублировать).
  claimSuggesting: {},
};

/* ── Работа с API ─────────────────────────────────────────────── */

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  // Повторяем только чтение: повтор POST мог бы создать второе дело,
  // если первый запрос дошёл, а ответ потерялся.
  const attempts = method === "GET" ? 4 : 1;

  let response = null;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      response = await fetch(API + path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      break;
    } catch {
      // Короткий обрыв — перезапуск службы или мигание сети. Это ещё не
      // «программы нет»: даём ей несколько секунд и пробуем снова.
      if (attempt < attempts) {
        await new Promise((resolve) => setTimeout(resolve, attempt * 1500));
      }
    }
  }

  if (!response) {
    throw new Error(
      "Нет связи с программой. Проверьте, что она запущена, и обновите страницу."
    );
  }

  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body.detail || "";
    } catch {
      // Ответ без JSON: остаётся общий текст ниже.
    }
    throw new Error(detail || "Программа не смогла выполнить запрос. Попробуйте ещё раз.");
  }

  if (response.status === 204) return null;
  return response.json();
}

/* ── Мелкие помощники ─────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);

function show(element, visible = true) {
  element.hidden = !visible;
}

function showAlert(message, ok = false) {
  const box = $("alert");
  box.textContent = message;
  box.className = ok ? "alert ok" : "alert";
  show(box, true);
  if (ok) {
    setTimeout(() => show(box, false), 5000);
  }
}

/** Очистить текст, чтобы показать его как есть, без разметки. */
function escapeHtml(text) {
  return (text || "").replace(/[&<>"]/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[ch]));
}

/** Русские склонения: 1 файл, 2 файла, 5 файлов. */
function plural(n, one, few, many) {
  const mod10 = Math.abs(n) % 10;
  const mod100 = Math.abs(n) % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/** Разряды в числах читаются легче: 2 184 знака. */
function formatNum(n) {
  return Number(n || 0).toLocaleString("ru-RU");
}

function chars(n) {
  return `${formatNum(n)} ${plural(n, "знак", "знака", "знаков")}`;
}

const NEEDS_REVIEW = "ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ";

/*
  Коды из данных показываются словами: статусы, уровни замечаний и
  типы документов приходят английскими кодами (REVIEW_REQUIRED, HIGH,
  CLAIM). Неизвестный код показывается как есть — типы документов
  расширяемы данными, и выдумывать перевод нельзя.
*/
const SEVERITY_TEXT = {
  CRITICAL: "критично",
  HIGH: "существенно",
  MEDIUM: "требует внимания",
  LOW: "незначительно",
};

const DRAFT_STATUS_TEXT = {
  DRAFT: "черновик",
  UNDER_REVIEW: "на проверке",
  VERIFIED: "проверен",
  ACCEPTED: "принят",
  REJECTED: "отклонён",
  REGENERATED: "перегенерирован",
};

const CASE_STATUS_TEXT = {
  NEW: "новое",
  PROCESSING: "в работе",
  DRAFT_READY: "проект готов",
  UNDER_REVIEW: "на проверке",
  CORRECTED: "исправлено",
  READY_FOR_APPROVAL: "готово к подписанию",
  ARCHIVED: "в архиве",
};

const DOC_TYPE_TEXT = {
  CLAIM: "Исковое заявление",
  RESPONSE: "Отзыв",
  CONTRACT: "Договор",
  RECEIPT: "Платёжный документ",
  ACT: "Акт",
  EXPERT_OPINION: "Заключение эксперта",
  EVIDENCE: "Доказательство",
  MOTION: "Ходатайство",
  PROTOCOL: "Протокол",
  OTHER: "Иной документ",
};

/*
  Статус утверждения — не украшение. «Не проверялось правилами» и
  «источник не найден» нельзя показывать одинаково: судья примет
  непроверенное за подтверждённое, а это ровно тот дефект, от которого
  продукт обязан защищать.
*/
const CLAIM_STATUS_TEXT = {
  SUPPORTED: "подтверждено",
  CONTRADICTED: "противоречит материалам",
  INSUFFICIENT_EVIDENCE: "источник не найден",
  NOT_APPLICABLE: "не проверялось правилами",
  HUMAN_REVIEW_REQUIRED: "нужна проверка юриста",
  VERIFICATION_ERROR: "проверка не выполнена",
};

/* Статус документа по проверке (ТЗ §14) — не путать с кодом отчёта. */
const DOCUMENT_STATUS_TEXT = {
  VERIFIED_WITHIN_SCOPE: "проверено в этих пределах",
  REVIEW_REQUIRED: "нужна проверка",
  BLOCKED: "есть блокирующие замечания",
  VERIFICATION_FAILED: "проверка не выполнена",
};

function label(map, key) {
  return map[key] || key;
}

/** Цвет чипа статуса дела — по смыслу, а не по коду. */
function caseStatusChip(status) {
  if (status === "READY_FOR_APPROVAL") return "green";
  if (status === "DRAFT_READY") return "teal";
  if (status === "UNDER_REVIEW") return "amber";
  return "grey";
}

/** Причина из фоновой задачи может быть технической — человеку нужна. */
function humanError(raw) {
  const text = String(raw || "").trim();
  if (!text) return "причина неизвестна. Попробуйте сформировать проект заново.";
  if (/error|traceback|exception|http \d{3}|exceptiongroup/i.test(text)) {
    console.error("Формирование упало:", text);
    return "что-то пошло не так на стороне программы. Попробуйте ещё раз; если повторится — перезапустите программу.";
  }
  return text;
}

function currentCaseObject() {
  return state.cases.find((item) => item.id === state.currentCase) || null;
}

/* ── Шаги: где пользователь и что дальше ──────────────────────── */

function renderSteps() {
  const hasCase = !!state.currentCase;
  const hasDocs = state.documents.length > 0;
  const hasDraft = !!state.draft;
  const current = !hasCase ? 0 : !hasDocs ? 1 : !hasDraft ? 2 : 3;
  const labels = [
    "Выбрать дело",
    "Добавить материалы",
    "Сформировать проект",
    "Проверить и подписать",
  ];

  $("steps").innerHTML = labels
    .map((text, index) => {
      const cls = index < current ? "step done" : index === current ? "step now" : "step";
      const num = index < current ? "✓" : String(index + 1);
      return `<span class="${cls}"><span class="n">${num}</span>${text}</span>`;
    })
    .join('<span class="arrow">→</span>');
}

/* ── Состояние сервиса ────────────────────────────────────────── */

async function loadHealth() {
  try {
    const info = await api("/api/info");
    $("app-ver").textContent = info.version
      ? `версия ${info.version} · интерфейс ${UI_BUILD}`
      : "";
    console.info(`CourtDraft UI ${UI_BUILD}`);
    $("health-dot").classList.remove("off");
    if (info.llm_provider === "mock") {
      $("health").textContent = "демонстрационный режим";
      showDemoBanner();
    } else {
      $("health").textContent = "программа готова";
    }
    updatePrivacyFooter(info.llm_provider);
    // Проверяем связь и дальше: страница может висеть открытой часами,
    // а службу за это время перезапустят.
    setTimeout(loadHealth, 60000);
  } catch {
    // Один неудачный запрос — не приговор: при перезапуске службы связь
    // пропадает на несколько секунд, и «нет связи» навсегда было бы
    // ложью. Пробуем снова, пока не ответит.
    $("health").textContent = "переподключение…";
    $("health-dot").classList.add("off");
    setTimeout(loadHealth, 5000);
  }
}

function updatePrivacyFooter(provider) {
  const footer = $("foot-privacy");
  if (!footer) return;
  if (provider === "openai") {
    footer.textContent =
      "Программа готова к работе.";
  } else if (provider === "ollama") {
    footer.textContent =
      "Данные и локальная модель работают на этом компьютере. " +
      "Материалы дела не отправляются во внешний API.";
  } else {
    footer.textContent =
      "Демонстрационный режим: реальная модель не вызывается.";
  }
}

/*
  Баннер демонстрационного режима.

  Слово «mock» в шапке заказчику ничего не объясняет, а молчаливый
  показ заглушек выглядит как поломка: судья увидит в разделах текст
  «[MOCK-ОТВЕТ]» и решит, что система не работает. Поэтому режим
  называется прямо и объясняется: что не подключено и что при этом
  работает по-настоящему.

  Показывается по факту состояния (/api/info отдаёт реальный
  провайдер), а не по флагу в разметке: иначе на машине с моделью
  баннер врал бы.
*/
function showDemoBanner() {
  const banner = $("demo-banner");
  if (!banner) return;
  banner.innerHTML =
    "<strong>Демонстрационный режим: языковая модель не подключена.</strong> " +
    "Резолютивная часть решения собирается по правилам и не зависит от модели " +
    "— её можно смотреть как есть. Остальные разделы не сформированы: вместо них " +
    "показана служебная заглушка с пометкой «заглушка». Проверка проекта честно " +
    "сообщает об этом, а не изображает готовый документ.";
  banner.hidden = false;
}

/* ── Дела ─────────────────────────────────────────────────────── */

async function loadCases() {
  const list = $("case-list");

  // Без кода список не запрашивается: молчаливый запрос без кода
  // вернул бы дела всех участников — ровно то, от чего защищаемся.
  if (!currentStudentCode()) {
    state.cases = [];
    list.innerHTML =
      '<p class="muted">Введите код участника — без него дела не показываются.</p>';
    renderSteps();
    return;
  }

  try {
    const data = await api(withStudentCode("/api/cases?limit=50"));
    state.cases = data.items || [];
    state.loadRetries = 0;
  } catch (error) {
    // Служба могла перезапускаться: не сдаёмся сразу, но и не долбим
    // сервер бесконечно.
    if (state.loadRetries < 3) {
      state.loadRetries += 1;
      list.innerHTML =
        '<p class="muted">Программа не отвечает. Переподключаемся…</p>';
      renderSteps();
      setTimeout(loadCases, 5000);
      return;
    }
    state.loadRetries = 0;
    list.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
    renderSteps();
    return;
  }

  if (!state.cases.length) {
    list.innerHTML = '<p class="muted">Дел пока нет. Создайте первое — кнопкой выше.</p>';
    renderSteps();
    return;
  }

  list.innerHTML = "";
  state.cases.forEach((item) => {
    const card = document.createElement("div");
    card.className = "case" + (item.id === state.currentCase ? " sel" : "");

    const count = item.document_count || 0;
    const chips = [
      `<span class="chip ${caseStatusChip(item.status)}">${escapeHtml(label(CASE_STATUS_TEXT, item.status))}</span>`,
      `<span class="chip grey">${count} ${plural(count, "файл", "файла", "файлов")}</span>`,
    ];
    // Вид судопроизводства определяется по материалам. Пока признаков
    // нет — чипа нет: пустой чип выглядел бы как ошибка интерфейса.
    if (item.case_type === "arbitration") chips.push('<span class="chip">арбитраж</span>');
    else if (item.case_type === "civil") chips.push('<span class="chip grey">гражданское</span>');
    if (item.is_synthetic) chips.push('<span class="chip bronze">демо</span>');

    card.innerHTML =
      `<div class="num">${escapeHtml(item.case_number)}</div>` +
      `<div class="court">${escapeHtml(item.court_name || "суд — из материалов")}</div>` +
      `<div class="row">${chips.join("")}</div>`;

    // Кнопка удаления внутри карточки: клик по ней не должен выбирать дело,
    // поэтому событие гасится. Подтверждение обязательно — удаление необратимо.
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "case-del";
    remove.title = "Удалить дело и все материалы";
    remove.setAttribute("aria-label", `Удалить дело ${item.case_number}`);
    remove.textContent = "×";
    remove.onclick = (event) => {
      event.stopPropagation();
      removeCase(item);
    };
    card.appendChild(remove);

    card.onclick = () => {
      selectCase(item.id);
    };
    list.appendChild(card);
  });

  renderSteps();
}

/* Удаление дела: спрашиваем подтверждение и честно говорим, что вернуть
 * нельзя. После удаления выбираем следующее дело, а не оставляем экран
 * с материалами уже несуществующего дела. */
async function removeCase(item) {
  const confirmed = window.confirm(
    `Удалить дело ${item.case_number} вместе со всеми материалами и проектом?\n\n` +
      "Вернуть его будет нельзя. Запись об удалении останется только в журнале действий."
  );
  if (!confirmed) return;

  const wasCurrent = state.currentCase === item.id;

  try {
    await api(`/api/cases/${item.id}`, { method: "DELETE" });
  } catch (error) {
    showAlert(`Дело не удалено: ${error.message}`);
    return;
  }

  if (wasCurrent) {
    // Незавершённые запросы по удалённому делу больше не нужны: иначе они
    // перерисуют экран данными, которых уже нет.
    state.loadSeq += 1;
    if (state.jobTimer) {
      clearInterval(state.jobTimer);
      state.jobTimer = null;
    }
    state.currentCase = null;
    state.loadingCaseId = null;
    state.documents = [];
    state.draft = null;
    resetDraftView();
    updateDocsLabel();
    $("doc-list").innerHTML = '<li class="muted">Дело не выбрано</li>';
    show($("progress"), false);
    $("btn-generate").disabled = true;
  }

  await loadCases();
  if (wasCurrent) {
    if (state.cases.length) {
      await selectCase(state.cases[0].id);
    } else {
      renderSteps();
    }
  }

  showAlert(`Дело ${item.case_number} удалено.`, true);
}

async function selectCase(caseId) {
  const seq = ++state.loadSeq;
  // Жёстко сбрасываем состояние предыдущего дела, чтобы не было подмешивания
  if (state.jobTimer) {
    clearInterval(state.jobTimer);
    state.jobTimer = null;
  }
  show($("progress"), false);
  $("progress-fill").classList.remove("busy");
  $("progress-fill").style.width = "8%";
  $("btn-generate").disabled = true;

  state.loadingCaseId = caseId;
  state.currentCase = caseId;
  state.documents = [];
  state.draft = null;

  // Сбрасываем вьюху проекта
  resetDraftView();
  // Форма иска помнит введённое по каждому делу отдельно.
  syncClaimForm();
  // Закрываем просмотр документа: иначе поверх нового дела остаётся
  // открытым документ предыдущего, и выглядит это как «данные смешались».
  show($("doc-viewer"), false);
  const savedAuthorName = savedAuthor();
  if ($("edit-author")) $("edit-author").value = savedAuthorName;
  if ($("wizard-author")) $("wizard-author").value = savedAuthorName;
  $("sections").innerHTML = '<p class="muted">Загрузка...</p>';
  $("doc-list").innerHTML = '<li class="muted">Загрузка...</li>';
  $("source-list").innerHTML = "";
  $("version-list").innerHTML = "";
  $("edit-text").value = "";
  $("edit-comment").value = "";
  $("m-sections").textContent = "0";
  $("m-issues").textContent = "0";
  $("m-sources").textContent = "0";
  $("m-versions").textContent = "0";
  $("metric-issues").classList.remove("alert");
  $("verdict").textContent = "";
  show($("sources"), false);
  show($("versions"), false);
  show($("verification"), false);
  show($("metrics"), false);
  show($("btn-download"), false);
  show($("export-mode"), false);
  show($("btn-feedback"), true);
  show($("editor"), false);
  show($("facts"), false);
  $("facts-count").textContent = "0";
  $("facts-body").innerHTML = "";
  $("facts-hint").textContent =
    "Суммы, даты, участники и требования — с цитатой из документа.";
  $("btn-facts").hidden = !factsEnabled();
  $("btn-facts").disabled = true;

  await loadCases();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  await loadDocuments();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  await loadDraft();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  $("btn-generate").disabled = !state.currentCase;
  updateGenerateButton();
  updateFactsButton();
  await loadFacts();
  loadPassport();
  loadDeviations();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  if (state.docKind === "claim") {
    void suggestClaimFromMaterials(false);
  }
  renderSteps();
}

/* Создание пустого дела: одно нажатие.

   Номер дела и суд больше не спрашиваются: в этих полях оказывалось
   что угодно (фамилия, «уц»), и проект расходился с карточкой. Реквизиты
   подставляет сервер из загруженных материалов. */
async function createCase() {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  const number =
    `б/н ${pad(now.getDate())}.${pad(now.getMonth() + 1)}.${now.getFullYear()} ` +
    `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;

  const created = await api("/api/cases", {
    method: "POST",
    body: JSON.stringify({
      case_number: number,
      court_name: "",
      category: "",
      is_synthetic: false,
      student_code: currentStudentCode(),
    }),
  });

  showAlert(
    state.cases.some((item) => item.id === created.id)
      ? "Возвращено ваше незаполненное дело. Добавьте материалы — номер и суд подставятся сами."
      : "Дело создано. Добавьте материалы — номер и суд подставятся сами.",
    true,
  );
  await loadCases();
  await selectCase(created.id);
}

/* Демо-дело: создаётся вместе с готовыми материалами для показа. */
async function createDemoCase() {
  const number = `DEMO-${Date.now().toString().slice(-5)}`;

  const created = await api("/api/cases", {
    method: "POST",
    body: JSON.stringify({
      case_number: number,
      court_name: "Демонстрационный районный суд",
      case_type: "civil",
      category: "договорные споры",
      is_synthetic: true,
      student_code: currentStudentCode(),
    }),
  });

  const manifest = await api("/api/demo/manifest").catch(() => null);
  const files = manifest ? manifest.documents : [];

  for (const file of files) {
    // Путь абсолютный: страница открывается как /app/, и относительный
    // «api/demo/...» ушёл бы в /app/api/demo/... — 404 и пустой документ.
    const text = await fetch(`${API}/api/demo/${encodeURIComponent(file.filename)}`)
      .then((r) => (r.ok ? r.text() : ""));
    await api(`/api/cases/${created.id}/documents`, {
      method: "POST",
      body: JSON.stringify({
        filename: file.filename,
        document_type: file.document_type,
        mime_type: "text/plain",
        text,
      }),
    });
  }

  showAlert(
    files.length
      ? `Создано дело ${number} с ${files.length} материалами.`
      : `Создано дело ${number}. Материалы не загружены.`,
    true
  );

  await selectCase(created.id);
}

/* ── Материалы дела ───────────────────────────────────────────── */

function updateDocsLabel() {
  const labelEl = $("docs-label");
  if (!state.currentCase) {
    labelEl.textContent = "Материалы дела";
    return;
  }
  const n = state.documents.length;
  labelEl.textContent = `Материалы дела — ${n} ${plural(n, "файл", "файла", "файлов")}`;
}

async function loadDocuments() {
  const list = $("doc-list");
  const caseId = state.currentCase || state.loadingCaseId;
  const seq = state.loadSeq;

  if (!caseId) {
    state.documents = [];
    list.innerHTML = '<li class="muted">Дело не выбрано</li>';
    updateDocsLabel();
    renderSteps();
    return;
  }

  try {
    const docs = await api(withStudentCode(`/api/cases/${caseId}/documents`));
    if (seq !== state.loadSeq) return;
    if (caseId !== state.currentCase || state.loadingCaseId !== caseId) return;
    state.documents = docs;
  } catch (error) {
    if (seq !== state.loadSeq) return;
    if (caseId !== state.currentCase || state.loadingCaseId !== caseId) return;
    list.innerHTML = `<li class="muted">${escapeHtml(error.message)}</li>`;
    updateDocsLabel();
    renderSteps();
    return;
  }

  updateDocsLabel();
  updateFactsButton();

  if (!state.documents.length) {
    list.innerHTML =
      '<li class="muted">Материалов нет. Без них проект не составить — перетащите файлы в окно выше.</li>';
    renderSteps();
    return;
  }

  list.innerHTML = "";
  state.documents.forEach((doc) => {
    const li = document.createElement("li");
    li.className = "doc";
    li.dataset.id = doc.id;

    const type = label(DOC_TYPE_TEXT, doc.document_type);
    const meta =
      `<span class="kind">${escapeHtml(type)}</span>` +
      (doc.text_source === "ocr"
        ? '<span class="kind ocr" title="Текст получен распознаванием скана: возможны ошибки OCR">распознано OCR</span>'
        : "") +
      (doc.injection_warning
        ? '<span class="kind warn" title="В тексте есть фразы, похожие на инструкции модели: они не выполняются, но документ стоит проверить">⚠ инструкции в тексте</span>'
        : "") +
      `<span>${escapeHtml(chars(doc.characters))}</span>`;

    li.innerHTML =
      '<span class="fic">📄</span>' +
      `<div><div class="nm">${escapeHtml(doc.filename)}</div>` +
      `<div class="meta">${meta}</div>` +
      (doc.has_placeholders
        ? '<div class="warnline">⚠ в файле скрыты личные данные (метки)</div>'
        : "") +
      `</div>`;
    li.onclick = () => openDoc(doc.id);
    list.appendChild(li);
  });

  renderSteps();
}

/* ── Загрузка файлов перетаскиванием ──────────────────────────── */

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  if (!state.currentCase) {
    showAlert("Сначала выберите или создайте дело — файлы попадают в него.");
    return;
  }

  // Дело запоминается один раз: иначе переключение на другое дело во
  // время загрузки отправит оставшиеся файлы уже в него — материалы
  // двух дел перемешаются.
  const caseId = state.currentCase;

  const drop = $("dropzone");
  const title = drop.querySelector(".t");
  const previousTitle = title.textContent;
  const loaded = [];
  const failed = [];
  const notes = [];

  drop.classList.add("busy");

  for (const file of files) {
    title.textContent = `Загружаем «${file.name}»…`;

    if (!file.size) {
      failed.push(`Файл «${file.name}» пустой — читать нечего.`);
      continue;
    }

    const form = new FormData();
    form.append("file", file, file.name);

    try {
      // Без заголовка Content-Type: браузер сам ставит границу multipart.
      const response = await fetch(
        API + withStudentCode(`/api/cases/${caseId}/documents/upload`),
        { method: "POST", body: form }
      );

      if (response.ok) {
        const body = await response.json().catch(() => ({}));
        loaded.push(file.name);
        (body.warnings || []).forEach((warning) => notes.push(warning));
      } else {
        let detail = `Не удалось загрузить «${file.name}».`;
        try {
          const body = await response.json();
          if (body.detail) detail = body.detail;
        } catch {
          // Нет JSON — остаётся общий текст.
        }
        failed.push(detail);
      }
    } catch {
      failed.push(
        `Программа не ответила — файл «${file.name}» не загружен. ` +
          "Подождите несколько секунд и обновите страницу: возможно, он уже сохранился."
      );
    }
  }

  drop.classList.remove("busy");
  title.textContent = previousTitle;
  $("file-input").value = "";

  await loadDocuments();
  // Счётчик файлов в карточке дела берётся из списка дел, поэтому обновляем
  // и его: иначе после загрузки карточка показывает старое число.
  await loadCases();

  if (loaded.length && !failed.length) {
    showAlert(
      `Загружено: ${loaded.join(", ")}. Материалы уже доступны проекту.` +
        (notes.length ? ` ${notes.join(" ")}` : ""),
      true
    );
  } else if (loaded.length) {
    showAlert(
      `Загружено ${loaded.length}. Проблемы: ${failed.join(" ")}` +
        (notes.length ? ` ${notes.join(" ")}` : "")
    );
  } else {
    showAlert(failed.join(" "));
  }
}

/* ── Просмотр документа ───────────────────────────────────────── */

/** Подсветить метки обезличивания: <ДАТА23> видно, но не редактируется. */
function highlightPlaceholders(text) {
  return escapeHtml(text).replace(
    /&lt;([А-ЯЁA-Za-z]{2,12}\d{1,4})&gt;/g,
    '<span class="hidden-val">&lt;$1&gt;</span>'
  );
}

async function openDoc(docId) {
  const doc = state.documents.find((item) => item.id === docId);
  if (!doc || !state.currentCase) return;

  $("doc-viewer-title").textContent = doc.filename;
  $("doc-viewer-meta").textContent = "Загрузка…";
  $("doc-viewer-body").innerHTML = '<p class="muted">Читаем документ…</p>';
  show($("doc-viewer"), true);

  try {
    const detail = await api(
      withStudentCode(`/api/cases/${state.currentCase}/documents/${docId}`)
    );
    const type = label(DOC_TYPE_TEXT, detail.document_type);
    const hidden = detail.placeholders.length
      ? ` · скрыто значений: ${detail.placeholders.length}`
      : "";
    $("doc-viewer-meta").textContent =
      `${type} · ${chars(detail.characters)}${hidden}` +
      (detail.text_source === "ocr"
        ? " · текст распознан OCR, возможны ошибки"
        : "");
    $("doc-viewer-body").innerHTML = detail.text
      ? highlightPlaceholders(detail.text)
      : '<span class="muted">В документе нет текста.</span>';
  } catch (error) {
    $("doc-viewer-body").innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

function closeDoc() {
  show($("doc-viewer"), false);
}

/* ── Исковое заявление (истец) ────────────────────────────────────
   Иск — не судебный акт: здесь сторона истца, и текст не должен
   звучать как решение. Данные вводит человек; цена, пошлина и
   подсудность считаются по введённому, а не моделью. Пустое поле
   остаётся пробелом и показывается как «не хватает данных».
*/

function docKind() {
  return state.docKind === "claim" ? "claim" : "judgment";
}

function updateGenerateButton() {
  const button = $("btn-generate");
  if (!state.currentCase) {
    button.disabled = true;
    return;
  }
  // Для иска материалы не обязательны: вход — данные формы.
  button.disabled = docKind() === "claim" ? false : !state.documents.length;
}

function switchDocKind(kind) {
  state.docKind = kind === "claim" ? "claim" : "judgment";
  if ($("doc-kind")) $("doc-kind").value = state.docKind;
  show($("claim-box"), state.docKind === "claim");
  $("btn-generate").textContent =
    state.docKind === "claim" ? "Сгенерировать иск" : "Сформировать решение";
  if (state.docKind === "claim") {
    syncClaimForm();
    renderClaimSuggest(state.claimSuggest[state.currentCase] || null);
    void suggestClaimFromMaterials(false);
  }
  resetDraftView();
  updateGenerateButton();
  renderSteps();
}

function demandRow(data = {}) {
  const row = document.createElement("div");
  row.className = "claim-row";
  row.dataset.row = "1";
  row.innerHTML =
    `<label>Требование<input data-f="description" type="text" autocomplete="off"></label>` +
    `<label>Сумма, ₽<input data-f="amount" type="text" inputmode="decimal" autocomplete="off"></label>` +
    `<label>Период с<input data-f="period_from" type="text" placeholder="ДД.ММ.ГГГГ" autocomplete="off"></label>` +
    `<label>Период по<input data-f="period_to" type="text" placeholder="ДД.ММ.ГГГГ" autocomplete="off"></label>` +
    `<label>Основание<input data-f="basis" type="text" autocomplete="off"></label>` +
    `<button class="btn ghost del" type="button" data-del title="Убрать строку">✕</button>`;
  setRowValues(row, data);
  row.querySelector("[data-del]").onclick = () => {
    row.remove();
    if (!$("claim-demands").children.length) addDemandRow();
  };
  return row;
}

function factRow(data = {}) {
  const row = document.createElement("div");
  row.className = "claim-row fact";
  row.dataset.row = "1";
  row.innerHTML =
    `<label>Дата<input data-f="date" type="text" placeholder="ДД.ММ.ГГГГ" autocomplete="off"></label>` +
    `<label>Обстоятельство<input data-f="text" type="text" autocomplete="off"></label>` +
    `<label>Документ-источник<input data-f="source" type="text" autocomplete="off"></label>` +
    `<button class="btn ghost del" type="button" data-del title="Убрать строку">✕</button>`;
  setRowValues(row, data);
  row.querySelector("[data-del]").onclick = () => {
    row.remove();
    if (!$("claim-facts").children.length) addFactRow();
  };
  return row;
}

function setRowValues(row, data) {
  ["description", "amount", "period_from", "period_to", "basis",
   "date", "text", "source"].forEach((name) => {
    const input = row.querySelector(`[data-f="${name}"]`);
    const value = data[name];
    if (input && value !== null && value !== undefined) input.value = String(value);
  });
}

function addDemandRow(data = {}) {
  $("claim-demands").appendChild(demandRow(data));
}

function addFactRow(data = {}) {
  $("claim-facts").appendChild(factRow(data));
}

function collectClaimData() {
  const val = (id) => ($(id) && $(id).value ? $(id).value.trim() : "");
  const rows = (box, mapper) =>
    [...box.querySelectorAll("[data-row]")].map(mapper);

  const demands = rows($("claim-demands"), (row) => {
    const f = (name) => (row.querySelector(`[data-f="${name}"]`) || {}).value || "";
    return {
      kind: "other",
      description: f("description").trim(),
      amount: f("amount").trim() || null,
      basis: f("basis").trim(),
      period_from: f("period_from").trim() || null,
      period_to: f("period_to").trim() || null,
    };
  }).filter((item) => item.description || item.amount);

  const facts = rows($("claim-facts"), (row) => {
    const f = (name) => (row.querySelector(`[data-f="${name}"]`) || {}).value || "";
    return {
      date: f("date").trim() || null,
      text: f("text").trim(),
      source_document: f("source").trim() || null,
    };
  }).filter((item) => item.text);

  const attachments = val("claim-attachments")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((name) => ({ name }));

  return {
    court_name: val("claim-court-name"),
    court_level: val("claim-court-level"),
    court_address: val("claim-court-address"),
    category: val("claim-category"),
    plaintiff: {
      name: val("claim-plaintiff-name"),
      address: val("claim-plaintiff-address"),
      representative: val("claim-plaintiff-rep"),
    },
    defendant: {
      name: val("claim-defendant-name"),
      address: val("claim-defendant-address"),
    },
    contract: {
      date: val("claim-contract-date") || null,
      number: val("claim-contract-number"),
      subject: val("claim-contract-subject"),
    },
    demands,
    facts,
    attachments,
    pretrial: {
      required: $("claim-pretrial-required").checked,
      sent: $("claim-pretrial-sent").checked,
      date: val("claim-pretrial-date") || null,
    },
  };
}

function applyClaimData(data) {
  const set = (id, value) => {
    if ($(id) && value !== null && value !== undefined) $(id).value = String(value);
  };
  const party = data.plaintiff || {};
  const foe = data.defendant || {};
  const contract = data.contract || {};
  const pretrial = data.pretrial || {};

  set("claim-court-name", data.court_name);
  set("claim-court-level", data.court_level);
  set("claim-court-address", data.court_address);
  set("claim-category", data.category);
  set("claim-plaintiff-name", party.name);
  set("claim-plaintiff-address", party.address);
  set("claim-plaintiff-rep", party.representative);
  set("claim-defendant-name", foe.name);
  set("claim-defendant-address", foe.address);
  set("claim-contract-date", contract.date);
  set("claim-contract-number", contract.number);
  set("claim-contract-subject", contract.subject);
  set("claim-pretrial-date", pretrial.date);
  $("claim-pretrial-required").checked = pretrial.required === true;
  $("claim-pretrial-sent").checked = pretrial.sent === true;
  $("claim-attachments").value = (data.attachments || [])
    .map((item) => item.name)
    .join("\n");

  $("claim-demands").innerHTML = "";
  (data.demands && data.demands.length ? data.demands : [{}]).forEach((item) =>
    addDemandRow(item)
  );
  $("claim-facts").innerHTML = "";
  (data.facts && data.facts.length ? data.facts : [{}]).forEach((item) =>
    addFactRow(item)
  );
}

function syncClaimForm() {
  const data = state.currentCase ? state.claimData[state.currentCase] : null;
  applyClaimData(data || {});
  renderClaimSuggest(state.currentCase ? (state.claimSuggest[state.currentCase] || null) : null);
  show($("claim-result"), false);
  show($("claim-alert"), false);
}

function jurisdictionWord(jurisdiction) {
  const status = (jurisdiction || {}).status;
  if (status === "ok") return "соответствует";
  if (status === "warn") return "требует проверки";
  return "не определена";
}

function renderClaimResult(result) {
  const box = $("claim-result");
  const fee = result.state_fee ? `${result.state_fee.amount_rub} ₽` : "не рассчитана";
  const jurisdiction = result.jurisdiction || {};
  const jurClass =
    jurisdiction.status === "ok" ? "ok" : jurisdiction.status === "warn" ? "bad" : "warn";
  const gaps = (result.gaps || []).length
    ? `<div class="gaps"><b>Не хватает данных:</b><ul>` +
      result.gaps.map((item) => `<li>${escapeHtml(item)}</li>`).join("") +
      `</ul></div>`
    : `<p class="muted small">Обязательных пробелов не найдено.</p>`;

  box.innerHTML =
    `<div class="claim-calc">` +
    `<span class="tag">Цена иска: <b>${escapeHtml(result.claim_price || "—")} ₽</b></span>` +
    `<span class="tag">Госпошлина: <b>${escapeHtml(fee)}</b></span>` +
    `<span class="tag ${jurClass}">Подсудность: ${escapeHtml(jurisdictionWord(jurisdiction))}</span>` +
    `</div>` +
    (jurisdiction.reason
      ? `<p class="muted small">${escapeHtml(jurisdiction.reason)}</p>`
      : "") +
    gaps +
    (result.limitation
      ? `<p class="muted small">Срок давности: ${escapeHtml(result.limitation.reason || result.limitation.status || "")}</p>`
      : "") +
    (result.pretrial && result.pretrial.reason
      ? `<p class="muted small">Досудебный порядок: ${escapeHtml(result.pretrial.reason)}</p>`
      : "") +
    `<details class="details-box" style="margin-top:10px">` +
    `<summary>Предварительный текст иска (шаблон ст. 131, 132 ГПК РФ)</summary>` +
    `<div class="claim-skeleton">${escapeHtml(result.skeleton || "")}</div>` +
    `</details>`;

  show(box, true);
}


function renderClaimSuggest(meta) {
  const box = $("claim-suggest");
  const msg = $("claim-suggest-msg");
  const miss = $("claim-suggest-missing");
  if (!box || !msg) return;
  if (!meta) {
    show(box, false);
    if (miss) show(miss, false);
    return;
  }
  msg.textContent = meta.message || "";
  if (miss) {
    const missing = meta.missing_fields || [];
    if (missing.length) {
      miss.textContent = "Не найдено в материалах, дозаполните: " + missing.join(", ") + ".";
      show(miss, true);
    } else {
      show(miss, false);
    }
  }
  show(box, true);
}

async function suggestClaimFromMaterials(force = false) {
  if (!state.currentCase || docKind() !== "claim") return;
  const caseId = state.currentCase;
  const existing = state.claimData[caseId];

  // Автозаполнение перезапишет данные только если форма ещё не правилась
  // вручную: иначе подставленные значения затёрли бы чужие правки.
  const canApply = force || !existing || existing._auto === true;
  if (!canApply && !force) {
    renderClaimSuggest(state.claimSuggest[caseId] || null);
    return;
  }

  if (state.claimSuggesting[caseId]) return;
  state.claimSuggesting[caseId] = true;
  const btn = $("btn-claim-suggest");
  const btn2 = $("btn-claim-suggest-bottom");
  if (btn) btn.disabled = true;
  if (btn2) btn2.disabled = true;

  try {
    const result = await api(`/api/cases/${caseId}/claim/suggest`, { method: "POST" });
    if (state.currentCase !== caseId) return;

    state.claimSuggest[caseId] = {
      filled_fields: result.filled_fields || [],
      missing_fields: result.missing_fields || [],
      message: result.message || "",
      sources: result.sources || [],
    };

    const data = result.claim_data || {};
    if (canApply) {
      data._auto = true;
      state.claimData[caseId] = data;
      applyClaimData(data);
    }

    renderClaimSuggest(state.claimSuggest[caseId]);
    updateGenerateButton();
  } catch (error) {
    renderClaimSuggest({
      message: "Не удалось заполнить из материалов: " + (error.message || "Ошибка"),
      missing_fields: [],
      filled_fields: [],
    });
  } finally {
    state.claimSuggesting[caseId] = false;
    if (btn) btn.disabled = false;
    if (btn2) btn2.disabled = false;
  }
}

async function previewClaim() {
  if (!state.currentCase) {
    showAlert("Сначала выберите дело.");
    return;
  }
  const data = collectClaimData();
  data._auto = false;
  state.claimData[state.currentCase] = data;
  show($("claim-alert"), false);

  const button = $("btn-claim-preview");
  button.disabled = true;
  try {
    const result = await api(`/api/cases/${state.currentCase}/claim`, {
      method: "POST",
      body: JSON.stringify(data),
    });
    renderClaimResult(result);
    updateGenerateButton();
  } catch (error) {
    const box = $("claim-alert");
    box.textContent = `Не удалось построить иск: ${error.message}`;
    show(box, true);
  } finally {
    button.disabled = false;
  }
}

/* ── Формирование проекта ─────────────────────────────────────── */

async function generate() {
  if (!state.currentCase) {
    showAlert("Сначала выберите дело.");
    return;
  }
  const isClaim = docKind() === "claim";

  let claimData = null;
  if (isClaim) {
    // Для иска вход — данные формы, а не загруженные материалы.
    claimData = collectClaimData();
    if (!claimData.demands.length) {
      showAlert("Добавьте хотя бы одно требование — без него иск не построить.");
      return;
    }
    if (!claimData.facts.length) {
      showAlert("Добавьте обстоятельства: иск без оснований не составляется.");
      return;
    }
    claimData._auto = false;
    state.claimData[state.currentCase] = claimData;
  } else if (!state.documents.length) {
    showAlert("Сначала добавьте материалы дела — без них проект не составить.");
    return;
  }

  const button = $("btn-generate");
  button.disabled = true;
  show($("progress"), true);
  $("progress-fill").style.width = "8%";
  $("progress-fill").classList.add("busy");
  $("progress-text").textContent = isClaim
    ? "Начинаем подготовку искового заявления…"
    : "Начинаем формирование проекта…";

  try {
    const body = isClaim
      ? {
          actor: "judge",
          document_kind: "claim",
          template_name: "civil_claim",
          claim_data: claimData,
        }
      : { actor: "judge" };
    const job = await api(`/api/cases/${state.currentCase}/draft`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    pollJob(job.job_id, state.currentCase);
  } catch (error) {
    stopProgress();
    showAlert(
      `Не удалось запустить ${isClaim ? "подготовку иска" : "формирование"}: ${error.message}`
    );
  }
}

function pollJob(jobId, caseId) {
  clearInterval(state.jobTimer);

  // Прогресс оценочный: бэкенд не сообщает процент готовности,
  // потому что модель не умеет его сообщать. Честнее показать движение,
  // чем выдумывать точные проценты.
  let ticks = 0;

  state.jobTimer = setInterval(async () => {
    // Пользователь мог переключиться на другое дело, пока шла генерация:
    // тогда опрос этого дела больше не нужен и его результат не должен
    // перерисовать чужую карточку.
    if (caseId && state.currentCase !== caseId) {
      stopProgress();
      return;
    }
    ticks += 1;

    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (caseId && state.currentCase !== caseId) {
        stopProgress();
        return;
      }
      // Бэкенд отдаёт оценку готовности и очередь: показываем её, а не
      // выдуманное «тиканье». Если оценка ещё нулевая — двигаем полоску
      // минимально, чтобы было видно, что запрос жив.
      const pct = typeof job.progress === "number" ? job.progress : 0;
      $("progress-fill").style.width = `${Math.max(4, Math.min(99, pct))}%`;
      $("progress-text").textContent = describeState(job, ticks);

      if (job.state === "DONE") {
        stopProgress();
        $("progress-fill").style.width = "100%";
        showAlert(
          docKind() === "claim"
            ? "Исковое заявление сформировано и проверено."
            : "Проект сформирован и проверен.",
          true
        );
        await loadDraft();
        // Статус дела в списке меняется вместе с проектом.
        await loadCases();
      } else if (job.state === "FAILED") {
        stopProgress();
        showAlert(`Формирование не удалось: ${humanError(job.error)}`);
      }
    } catch (error) {
      stopProgress();
      showAlert(`Не удалось узнать состояние задачи: ${error.message}`);
    }
  }, POLL_INTERVAL);
}

function describeState(job, ticks) {
  const state = job.state;
  const seconds = ticks * (POLL_INTERVAL / 1000);
  const what = docKind() === "claim" ? "Модель пишет иск" : "Модель пишет проект";
  if (state === "PENDING") {
    const position = job.queue_position;
    const eta = job.eta_seconds;
    if (position != null && eta != null) {
      return `В очереди: ${position + 1}-й, осталось примерно ${formatDuration(eta)}`;
    }
    return "Задача в очереди…";
  }
  if (state === "RUNNING") {
    if (job.eta_seconds) {
      return `${what}… осталось примерно ${formatDuration(job.eta_seconds)}`;
    }
    return `${what}… ${seconds} с. На процессоре это 2–3 минуты.`;
  }
  return docKind() === "claim" ? "Готовим иск…" : "Готовим проект…";
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} с`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  return `${hours} ч ${minutes % 60} мин`;
}

function stopProgress() {
  clearInterval(state.jobTimer);
  state.jobTimer = null;
  $("progress-fill").classList.remove("busy");
  show($("progress"), false);
  updateGenerateButton();
}

/* ── Показ проекта ────────────────────────────────────────────── */

function resetDraftView() {
  const isClaim = docKind() === "claim";
  $("draft-title").textContent = isClaim ? "Иск не сформирован" : "Проект не сформирован";
  const statusChip = $("draft-status");
  statusChip.hidden = true;
  statusChip.textContent = "";
  statusChip.className = "chip";
  $("draft-meta").textContent = isClaim
    ? "Заполните данные иска и нажмите «Сгенерировать иск»."
    : "Нажмите «Сформировать проект». Это займёт две-три минуты.";
  $("m-sections").textContent = "0";
  $("m-issues").textContent = "0";
  $("metric-issues").classList.remove("alert");
  $("verdict").textContent = "";
  $("m-sources").textContent = "0";
  $("m-versions").textContent = "0";
  show($("metrics"), false);
  show($("verification"), false);
  show($("sources"), false);
  show($("versions"), false);
  show($("editor"), false);
  show($("btn-download"), false);
  show($("export-mode"), false);
  $("sections").innerHTML =
    '<p class="muted">Разделы проекта появятся здесь.</p>';
  $("source-list").innerHTML = "";
  $("version-list").innerHTML = "";
}

async function loadDraft() {
  const caseId = state.currentCase || state.loadingCaseId;
  const seq = state.loadSeq;

  if (!caseId) {
    state.draft = null;
    resetDraftView();
    renderSteps();
    return;
  }

  let draft;
  try {
    draft = await api(`/api/cases/${caseId}/draft`);
  } catch (error) {
    if (seq !== state.loadSeq) return;
    if (caseId !== state.currentCase || state.loadingCaseId !== caseId) return;
    showAlert(`Не удалось получить проект: ${error.message}`);
    return;
  }

  if (seq !== state.loadSeq) return;
  if (caseId !== state.currentCase || state.loadingCaseId !== caseId) return; // Переключились на другое дело — игнорируем ответ
  state.draft = draft;
  if (!draft) {
    resetDraftView();
    renderSteps();
    return;
  }

  const caseItem = currentCaseObject();
  const isClaimDraft = (draft.document_kind || "judgment") === "claim";
  $("draft-title").textContent = caseItem
    ? `${isClaimDraft ? "Проект иска" : "Проект решения"} по делу ${caseItem.case_number}`
    : isClaimDraft
      ? "Проект иска"
      : "Проект решения";

  const statusChip = $("draft-status");
  statusChip.hidden = false;
  statusChip.textContent = label(DRAFT_STATUS_TEXT, draft.status);
  const verified = draft.status === "VERIFIED" || draft.status === "ACCEPTED";
  statusChip.className = "chip " + (verified ? "green" : draft.status === "REJECTED" ? "red" : "amber");

  $("draft-meta").textContent =
    `${isClaimDraft ? "исковое заявление" : "решение суда"} · ` +
    `версия ${draft.current_version} · разделов ${(draft.sections || []).length} · ` +
    `источников ${(draft.sources || []).length}`;

  show($("metrics"), true);
  show($("btn-download"), true);
  show($("export-mode"), true);
  $("m-sections").textContent = formatNum((draft.sections || []).length);

  /*
    Утверждения запрашиваются отдельно, после показа проекта: сбой этого
    запроса не должен прятать сам проект, но и молчать о нём нельзя —
    иначе пустой список выглядит как «нечего проверять».
  */
  let claims = null;
  let claimsError = "";
  try {
    claims = await api(`/api/drafts/${draft.id}/claims`);
  } catch (error) {
    claimsError = error.message || "проверка утверждений недоступна";
  }
  if (seq !== state.loadSeq) return;

  renderSections(draft, claims, claimsError);
  renderVerification(draft.verification);
  updateDownloadButton();
  renderSources(draft.sources || []);
  await loadVersions();

  $("edit-text").value = draft.text || "";
  show($("editor"), true);
  show($("suggest-panel"), true);
  renderSteps();
}

/* ── Мини-агент дозаполнения: пошаговый wizard ──────────────────────── */

const SUGGEST = { spots: [], index: 0, selected: null, draftId: null };

const AUTHOR_KEY = "courtdraft.editor.author";
function savedAuthor() {
  try { return localStorage.getItem(AUTHOR_KEY) || ""; } catch { return ""; }
}
function rememberAuthor(name) {
  try { localStorage.setItem(AUTHOR_KEY, name); } catch { /* приватный режим */ }
}

function suggestRenderStep() {
  const total = SUGGEST.spots.length;
  if (!total || SUGGEST.index >= total) return;
  const spot = SUGGEST.spots[SUGGEST.index];
  SUGGEST.selected = null;
  $("wizard-progress").textContent = `Проблема ${SUGGEST.index + 1} из ${total}`;
  $("wizard-title").textContent = spot.section
    ? `Раздел: ${spot.section}`
    : "Спорное место";
  const variants = (spot.variants || [])
    .map(
      (v) => `<button type="button" class="wizard-variant" data-variant="${v.index}">
        <span class="wizard-variant-num">${v.index + 1}</span>
        <span class="wizard-variant-text">${escapeHtml(v.text || "")}</span>
      </button>`
    )
    .join("");
  $("wizard-body").innerHTML = `
    <p class="muted small">${escapeHtml(spot.reason || "")}</p>
    <div class="wizard-original">Было: ${escapeHtml(spot.original || "")}</div>
    <div class="wizard-context">${escapeHtml(spot.context || "")}</div>
    <div class="wizard-variants">${
      variants || '<p class="muted small">Варианты не получены — напишите свой.</p>'
    }</div>
    <label class="wizard-custom-label">Свой вариант
      <textarea id="wizard-custom" rows="3" placeholder="Напишите свою формулировку…"></textarea>
    </label>`;
  $("wizard-body")
    .querySelectorAll(".wizard-variant")
    .forEach((btn) => {
      btn.addEventListener("click", () => {
        SUGGEST.selected = Number(btn.dataset.variant);
        $("wizard-body")
          .querySelectorAll(".wizard-variant")
          .forEach((b) => b.classList.remove("selected"));
        btn.classList.add("selected");
      });
    });
}

async function showSuggestions() {
  const draft = state.draft;
  if (!draft || !draft.id) {
    $("suggest-status").textContent = "Сначала сформируйте проект.";
    return;
  }
  if (docKind() === "claim") {
    $("suggest-status").textContent =
      "Для иска правки вносятся через «Паспорт дела», а не строками.";
    return;
  }
  $("suggest-status").textContent = "Подключаюсь к модели…";
  try {
    // Поток событий: сервер отдаёт прогресс после каждого спорного места,
    // чтобы было видно «Обработано 3 из 11», а не молчаливое ожидание.
    const resp = await fetch(API + `/api/drafts/${draft.id}/suggest-edits-stream`, {
      method: "POST",
    });
    if (!resp.ok || !resp.body) throw new Error(`сервер вернул ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let spots = [];
    let lastDone = 0, total = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line.slice(6)); } catch { continue; }
        if (msg.error) throw new Error(msg.error);
        if (msg.total) total = msg.total;
        if (msg.done != null) lastDone = msg.done;
        if (total) {
          $("suggest-status").textContent =
            `Проверяем место ${lastDone} из ${total}: модель подбирает формулировки…`;
        }
        if (msg.spots) spots = msg.spots;
      }
    }

    if (!spots.length) {
      $("suggest-status").textContent =
        "Спорных мест не найдено — всё подтверждено источниками.";
      return;
    }
    SUGGEST.spots = spots;
    SUGGEST.index = 0;
    SUGGEST.total = spots.length;
    SUGGEST.draftId = draft.id;
    const authorInput = $("wizard-author");
    if (authorInput) authorInput.value = savedAuthor();
    $("suggest-status").textContent = `Готово. Найдено мест: ${spots.length}`;
    $("suggest-wizard").hidden = false;
    loadGaps(draft.id);
    suggestRenderStep();
  } catch (error) {
    $("suggest-status").textContent = "";
    showAlert(`Не удалось получить предложения: ${error.message}`);
  }
}

async function suggestWizardAccept() {
  const spot = SUGGEST.spots[SUGGEST.index];
  if (!spot) return;
  const authorName = ($("wizard-author")?.value || "").trim();
  if (authorName.length < 3) {
    showAlert("Укажите ФИО того, кто вносит правку (поле вверху мастера).");
    $("wizard-author")?.focus();
    return;
  }
  rememberAuthor(authorName);
  const custom = ($("wizard-custom")?.value || "").trim();
  let payload = null;
  if (custom.length >= 4) {
    payload = { spot_id: spot.spot_id, custom_text: custom, author: "judge", author_name: authorName };
  } else if (SUGGEST.selected != null) {
    const variant = (spot.variants || []).find(
      (v) => v.index === SUGGEST.selected
    );
    if (variant) {
      payload = {
        spot_id: spot.spot_id,
        variant_index: variant.index,
        variant_text: variant.text,
        author: "judge",
        author_name: authorName,
      };
    }
  }
  if (!payload) {
    showAlert("Выберите вариант или напишите свой (не менее 4 знаков).");
    return;
  }
  try {
    await api(`/api/drafts/${SUGGEST.draftId}/apply-edit`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    // Раньше после каждого принятия вызывался тяжёлый /suggest-edits —
    // он делал 9+ вызовов LLM и интерфейс «зависал» на минуту.
    // Теперь просто убираем принятое место из локального списка: быстро.
    const acceptedId = spot.spot_id;
    SUGGEST.spots = SUGGEST.spots.filter((s) => s.spot_id !== acceptedId);
    const remaining = SUGGEST.spots;
    const done = SUGGEST.total - remaining.length;
    SUGGEST.index = 0;
    SUGGEST.selected = null;
    if (!remaining.length) {
      $("suggest-wizard").hidden = true;
      $("suggest-status").textContent =
        `Обработано спорных мест: ${done}. Правки сохранены как новые версии. ` +
        `ФИО внёсшего: ${authorName}. Обновите текст ниже.`;
      await loadDraft();
    } else {
      $("wizard-progress").textContent = `Проблема 1 из ${remaining.length}`;
      suggestRenderStep();
    }
  } catch (error) {
    showAlert(`Не удалось применить: ${error.message}`);
  }
}

function suggestWizardSkip() {
  SUGGEST.index += 1;
  if (SUGGEST.index >= SUGGEST.spots.length) {
    $("suggest-wizard").hidden = true;
    $("suggest-status").textContent = "Проход завершён.";
    loadDraft();
    return;
  }
  suggestRenderStep();
}

async function loadGaps(draftId) {
  const box = $("wizard-gaps");
  if (!box) return;
  try {
    const data = await api(`/api/drafts/${draftId}/gaps`);
    const gaps = data.gaps || [];
    box.innerHTML = gaps.length
      ? `<div class="wizard-gaps-head">Вопросы по пробелам${data.blocked ? " — документ заблокирован" : ""}</div>` +
        gaps
          .map(
            (gap) => `<div class="wizard-gap">
              <div class="wizard-gap-q">${escapeHtml(gap.question)}${gap.blocking ? " <span class=\"chip red\">обязательно</span>" : ""}</div>
              <div class="wizard-gap-reason">${escapeHtml(gap.reason)}</div>
              <div class="wizard-gap-options">
                ${gap.options
                  .map(
                    (option) => `<button class="btn ghost small" type="button" data-gap="${escapeHtml(gap.gap_id)}" data-answer="${escapeHtml(option)}">${escapeHtml(option)}</button>`
                  )
                  .join("")}
              </div>
              ${gap.answered ? `<div class="muted small">Ответ: ${escapeHtml(gap.answer)}</div>` : ""}
            </div>`
          )
          .join("")
      : "";
    box.querySelectorAll("button[data-gap]").forEach((btn) => {
      btn.onclick = async () => {
        await api(`/api/drafts/${draftId}/gaps/answer`, {
          method: "POST",
          body: JSON.stringify({
            gap_id: btn.dataset.gap,
            answer: btn.dataset.answer,
            author: "judge",
          }),
        });
        await loadGaps(draftId);
        await loadDraft();
      };
    });
  } catch (error) {
    box.innerHTML = "";
  }
}

function initSuggestWizard() {
  $("wizard-close").onclick = () => {
    $("suggest-wizard").hidden = true;
    loadDraft();
  };
  $("wizard-accept").onclick = suggestWizardAccept;
  $("wizard-skip").onclick = suggestWizardSkip;
  $("suggest-wizard").addEventListener("click", (event) => {
    if (event.target === $("suggest-wizard")) {
      $("suggest-wizard").hidden = true;
      loadDraft();
    }
  });
}

function claimChipClass(status) {
  if (status === "SUPPORTED") return "green";
  if (status === "CONTRADICTED" || status === "VERIFICATION_ERROR") return "red";
  if (status === "INSUFFICIENT_EVIDENCE") return "amber";
  return "grey";
}

/*
  Одно утверждение: статус, текст, документ и цитата. Цитата показывается
  прямо здесь, а не ссылкой на другой экран: подтверждение, до которого
  надо кликать, судья не откроет.
*/
function claimHtml(claim) {
  const status = claim.support_status || "";
  const chip =
    `<span class="chip ${claimChipClass(status)}">` +
    `${escapeHtml(label(CLAIM_STATUS_TEXT, status))}</span>`;

  const references = claim.evidence || [];
  let sources = "";
  if (references.length) {
    sources =
      `<div class="cl-src">` +
      references
        .map(
          (reference) =>
            `<div class="cl-ref"><b>${escapeHtml(reference.document || "документ не указан")}</b>` +
            (reference.quote
              ? `<q>${escapeHtml(reference.quote)}</q>`
              : '<span class="muted"> цитата не найдена</span>') +
            (reference.locator
              ? `<span class="cl-loc">${escapeHtml(reference.locator)}</span>`
              : "") +
            `</div>`
        )
        .join("") +
      `</div>`;
  } else if (status === "NOT_APPLICABLE") {
    // «Не проверялось правилами» и «источник не найден» — разные факты:
    // в утверждении нет суммы или даты, сверять правилами нечего.
    sources =
      `<div class="cl-src none">Правилами не проверялось: в утверждении нет ` +
      `суммы или даты. Источник не подтверждён — проверьте по материалам дела.</div>`;
  } else {
    sources =
      `<div class="cl-src none">Источник не найден — утверждение нельзя ` +
      `считать подтверждённым.</div>`;
  }

  const reason =
    claim.review_reason && !references.length
      ? `<div class="cl-reason">${escapeHtml(claim.review_reason)}</div>`
      : "";

  return (
    `<li class="cl">${chip}<span class="cl-t">${escapeHtml(claim.text || "")}</span>` +
    `${sources}${reason}</li>`
  );
}

/** Утверждения одного раздела: их видно рядом с абзацем, к которому они относятся. */
function claimsBlock(sectionClaims) {
  if (!sectionClaims.length) return "";
  const supported = sectionClaims.filter(
    (claim) => claim.support_status === "SUPPORTED"
  ).length;
  const attention = sectionClaims.length - supported;
  return (
    `<div class="claims"><div class="cl-h">Утверждения` +
    `<span>подтверждено ${supported} · требуют проверки ${attention}</span></div>` +
    `<ul>${sectionClaims.map(claimHtml).join("")}</ul></div>`
  );
}

function renderSections(draft, claims, claimsError) {
  const box = $("sections");
  box.innerHTML = "";

  /*
    Утверждения приходят одним списком с полем section: разбивка по
    разделам делается здесь, чтобы у раздела не было двух источников
    правды о том, к чему относится абзац.
  */
  const claimsBySection = {};
  ((claims && claims.claims) || []).forEach((claim) => {
    const key = claim.section || "";
    (claimsBySection[key] = claimsBySection[key] || []).push(claim);
  });
  const completeness = claimsBySection.completeness || [];
  delete claimsBySection.completeness;

  // Отказ запроса — отдельная строка, а не пустой список: «утверждений
  // нет» и «проверка не ответила» — разные утверждения.
  if (claimsError) {
    const note = document.createElement("p");
    note.className = "claims-error";
    note.textContent =
      `Проверка утверждений недоступна: ${claimsError}. ` +
      "Разделы показаны без источников.";
    box.appendChild(note);
  }

  /*
    Замечания группируются по имени раздела: судья читает замечание
    рядом с абзацем, к которому оно относится, а не списком перед
    текстом.
  */
  const issues = (draft.verification && draft.verification.issues) || [];
  const bySection = {};
  issues.forEach((issue) => {
    const key = issue.section || "";
    (bySection[key] = bySection[key] || []).push(issue);
  });
  const matched = {};
  const matchedClaims = {};

  const remarkHtml = (issue) => {
    const important = issue.severity === "HIGH" || issue.severity === "CRITICAL";
    const word = important ? "Важно" : "Проверить";
    return (
      `<div class="remark${important ? "" : " a"}">` +
      `<b>${word}.</b> ${escapeHtml(issue.reason)}` +
      (issue.text ? `<span class="frag">${escapeHtml(issue.text)}</span>` : "") +
      `</div>`
    );
  };

  (draft.sections || []).forEach((section, index) => {
    matched[section.name] = true;
    // Проверка иска привязывает замечания к ключу раздела (facts, request),
    // проверка решения — к имени. Учитываем оба варианта, иначе замечание
    // уйдёт в «не привязано к разделу».
    const ownIssues = bySection[section.name] || bySection[section.key] || [];
    matched[section.key] = true;
    const sectionKey = section.key || section.name;
    const ownClaims = claimsBySection[sectionKey] || [];
    if (ownClaims.length) matchedClaims[sectionKey] = true;

    const chips = [];
    const st = section.status || (section.needs_review ? "review" : section.is_mock ? "review" : "ready");
    if (st === "blocked") chips.push('<span class="chip red">блокирующая ошибка</span>');
    else if (st === "review") chips.push('<span class="chip amber">нужна проверка</span>');
    else chips.push('<span class="chip green">готово</span>');

    const article = document.createElement("article");
    article.className = "sec" + (st === "blocked" ? " blocked" : "") + (st === "review" ? " review" : "") + (section.is_mock ? " stub" : "");
    // Активный раздел — тот, на котором пользователь stopped взгляд.
    // Плавающий микрофон диктовки привязывается именно к нему.
    article.dataset.sectionName = section.name || "";
    article.dataset.sectionKey = sectionKey || "";
    article.addEventListener("click", () => {
      box.querySelectorAll("article.sec").forEach((a) => a.classList.remove("active"));
      article.classList.add("active");
      const name = section.name || "";
      if ($("dictate-section")) $("dictate-section").textContent = name;
      if ($("dictate-context")) $("dictate-context").hidden = false;
      rememberActiveSection(name);
    });
    // Чего не хватает в материалах — отдельным блоком, а не строкой
    // внутри текста: в проекте акта не должно стоять «ТРЕБУЕТ
    // ПРОВЕРКИ СПЕЦИАЛИСТОМ», но судья обязан видеть, каких данных
    // в деле нет.
    const gaps =
      section.gaps && section.gaps.length
        ? `<div class="gaps"><b>Не хватает данных:</b><ul>` +
          section.gaps.map((g) => `<li>${escapeHtml(g)}</li>`).join("") +
          `</ul></div>`
        : "";
    article.innerHTML =
      `<div class="sh"><span class="no">${index + 1}</span>` +
      `<span class="st">${escapeHtml(section.name)}</span>` +
      `${chips.join("")}</div>` +
      `<div class="body">${gaps}<div class="txt">${escapeHtml(section.text || "")}</div>` +
      ownIssues.map(remarkHtml).join("") +
      claimsBlock(ownClaims) +
      (section.citations && section.citations.length
        ? `<div class="cites">Источники: ${escapeHtml(section.citations.join(", "))}</div>`
        : `<div class="cites">Источник не указан — раздел требует проверки.</div>`) +
      `</div>`;

    box.appendChild(article);
  });

  // Требования из материалов, не попавшие в проект вовсе: их нельзя
  // прятать внутрь разделов — их там нет.
  if (completeness.length) {
    const missingBlock = document.createElement("div");
    missingBlock.className = "claims completeness";
    missingBlock.innerHTML =
      `<div class="cl-h">Не отражено в проекте` +
      `<span>требований из материалов: ${completeness.length}</span></div>` +
      `<ul>${completeness.map(claimHtml).join("")}</ul>`;
    box.appendChild(missingBlock);
  }

  // Утверждения, не привязанные к разделу, показываются ниже списка:
  // потерянное утверждение выглядит как его отсутствие.
  const unattached = Object.keys(claimsBySection)
    .filter((key) => !matchedClaims[key])
    .flatMap((key) => claimsBySection[key]);
  if (unattached.length) {
    const rest = document.createElement("div");
    rest.className = "unsectioned";
    rest.innerHTML =
      '<p class="muted small">Эти утверждения проверка не привязала ' +
      "к разделу:</p>" +
      `<ul class="claims-list">${unattached.map(claimHtml).join("")}</ul>`;
    box.appendChild(rest);
  }

  // Замечания без привязки к разделу — после текста, а не перед ним.
  const leftover = [];
  Object.keys(bySection).forEach((key) => {
    if (!matched[key]) leftover.push(...bySection[key]);
  });
  if (leftover.length) {
    const rest = document.createElement("div");
    rest.className = "unsectioned";
    rest.innerHTML =
      '<p class="muted small">Эти замечания проверка не привязала ' +
      "к конкретному разделу:</p>" +
      leftover.map(remarkHtml).join("");
    box.appendChild(rest);
  }

  if ((draft.sections || []).some((s) => (s.text || "").includes(NEEDS_REVIEW))) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent =
      "Пометка «требует проверки» означает, что подтвердить утверждение " +
      "по материалам дела не удалось.";
    box.appendChild(note);
  }
}

function renderVerification(verification) {
  const box = $("verification");
  if (!verification) {
    show(box, false);
    return;
  }

  const count = verification.issues.length;
  const important = verification.issues.filter(
    (issue) => issue.severity === "HIGH" || issue.severity === "CRITICAL"
  ).length;

  // Счётчик замечаний: красный, когда есть важные.
  $("m-issues").innerHTML = important
    ? `${formatNum(count)} <small>${important} ${plural(important, "важное", "важных", "важных")}</small>`
    : formatNum(count);
  $("metric-issues").classList.toggle("alert", important > 0);

  let text;
  if (verification.status === "PASS") {
    box.className = "verify ok";
    text =
      `Проверка пройдена: ${verification.sections_checked} разделов сверены ` +
      "с материалами дела, замечаний нет.";
  } else if (verification.status === "BLOCKED") {
    box.className = "verify bad";
    text =
      `Есть важные замечания (${count}). ` +
      "Пока они не исправлены, проект подписывать нельзя.";
  } else {
    box.className = "verify";
    text =
      `Проверка нашла ${count} ${plural(count, "замечание", "замечания", "замечаний")}. ` +
      "Они помечены цветом в тексте разделов — прочитайте и решите, что исправить.";
  }

  if (verification.unsupported_claims) {
    text +=
      ` Утверждений без ссылки на материалы: ${verification.unsupported_claims}.`;
  }

  $("verdict").textContent = text;
  show(box, true);
  renderCriticalBanner(verification);

  // Отчёт прошлой версии: текст изменился, проверка — нет. Показывать
  // его как действующий нельзя: судья решит, что текущий текст проверен.
  if (verification.is_stale) {
    box.className = "verify stale";
    $("verdict").textContent =
      "Отчёт относится к прежней версии текста: после правки нужна " +
      "повторная проверка. " +
      text;
  }
}

function renderSources(sources) {
  const list = $("source-list");
  list.innerHTML = "";
  sources.forEach((source) => {
    const li = document.createElement("li");
    li.textContent = source;
    list.appendChild(li);
  });
  if (sources.length) {
    $("m-sources").textContent = formatNum(sources.length);
    show($("sources"), true);
  } else {
    $("m-sources").textContent = "не привязаны";
    $("m-sources").classList.add("warn");
    show($("sources"), false);
  }
}

async function loadVersions() {
  const caseId = state.currentCase;
  if (!state.draft || !caseId) return;

  let data;
  try {
    data = await api(`/api/cases/${caseId}/draft/versions?kind=${encodeURIComponent(docKind())}`);
  } catch {
    if (caseId !== state.currentCase) return;
    show($("versions"), false);
    return;
  }
  if (caseId !== state.currentCase || !state.draft) return;

  const list = $("version-list");
  list.innerHTML = "";

  data.versions.forEach((version) => {
    const li = document.createElement("li");
    const current = version.version === data.current_version ? "v-current" : "";
    li.className = current;
    li.textContent =
      `версия ${version.version} · автор ${authorName(version.author)} · ` +
      `${formatNum(version.text.length)} симв. · ${formatTime(version.created_at)}` +
      (version.comment ? ` · ${version.comment}` : "");
    list.appendChild(li);
  });

  $("m-versions").textContent = formatNum(data.versions.length);
  show($("versions"), data.versions.length > 0);
}

function authorName(author) {
  return { ai: "модель", secretary: "секретарь", judge: "судья" }[author] || author;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return date.toLocaleString("ru-RU", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

/* ── Скачивание текста ────────────────────────────────────────── */

async function downloadDraft() {
  if (!state.draft) return;
  const mode = ($("export-mode") && $("export-mode").value) || "draft";
  const btn = $("btn-download");
  btn.disabled = true;
  try {
    const resp = await fetch(
      API + `/api/drafts/${state.draft.id}/export-docx?mode=${encodeURIComponent(mode)}`,
      { method: "POST" }
    );
    if (resp.status === 409) {
      let detail = "Выгрузка заблокирована проверкой.";
      try { const body = await resp.json(); if (body.detail) detail = body.detail; } catch {}
      showAlert(detail);
      return;
    }
    if (!resp.ok) throw new Error(`сервер вернул ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const cd = resp.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="([^"]+)"/);
    link.download = m ? m[1] : "document.docx";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showAlert(mode === "final"
      ? "Финальный документ скачан (дисклеймер в свойствах файла)."
      : "Черновик скачан (с дисклеймером в тексте).", true);
  } catch (error) {
    showAlert(`Не удалось скачать документ: ${error.message}`);
  } finally {
    updateDownloadButton();
  }
}

function updateDownloadButton() {
  const btn = $("btn-download");
  if (!btn) return;
  const v = state.draft && state.draft.verification;
  const blocked = !!v && ((v.p0_count || 0) > 0 || v.is_stale);
  btn.disabled = blocked;
  btn.title = blocked
    ? "Выгрузка заблокирована: есть критические замечания или отчёт устарел"
    : "Скачать документ в Word (DOCX)";
}

/* ── Правка ───────────────────────────────────────────────────── */

async function saveEdit() {
  if (!state.draft) return;
  if (docKind() === "claim") {
    showAlert("Текст иска правится через паспорт дела, а не вручную.");
    return;
  }

  const text = $("edit-text").value.trim();
  if (!text) {
    showAlert("Текст правки пустой.");
    return;
  }
  const authorName = ($("edit-author").value || "").trim();
  if (authorName.length < 3) {
    showAlert("Укажите ФИО того, кто вносит правку.");
    $("edit-author").focus();
    return;
  }
  rememberAuthor(authorName);

  try {
    const version = await api(`/api/drafts/${state.draft.id}/versions`, {
      method: "POST",
      body: JSON.stringify({
        text,
        author: "judge",
        author_name: authorName,
        comment: $("edit-comment").value,
      }),
    });

    showAlert(
      `Сохранена версия ${version.version}. Правку внёс: ${authorName}.`,
      true
    );
    $("edit-comment").value = "";
    await loadDraft();
  } catch (error) {
    showAlert(`Не удалось сохранить правку: ${error.message}`);
  }
}

/*
  Повторная проверка. Кнопка не подтверждает проект: она запускает
  проверку, и статус появляется только из её результата.
*/
async function reverify() {
  if (!state.draft) return;
  const button = $("btn-reverify");
  button.disabled = true;
  try {
    const report = await api(`/api/drafts/${state.draft.id}/reverify`, {
      method: "POST",
    });
    showAlert(
      `Проверка выполнена: ${label(DOCUMENT_STATUS_TEXT, report.document_status)}.`,
      true
    );
    await loadDraft();
  } catch (error) {
    showAlert(`Не удалось проверить заново: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

/* ── Связка элементов ─────────────────────────────────────────── */

$("btn-generate").onclick = generate;
$("btn-save-edit").onclick = saveEdit;
$("btn-reverify").onclick = reverify;
$("btn-download").onclick = downloadDraft;
$("btn-suggest").onclick = showSuggestions;
initSuggestWizard();

/* Создание дела — одно нажатие, без формы. */
$("btn-new-case").onclick = async () => {
  if (!currentStudentCode()) {
    studentGateOpen();
    return;
  }
  const button = $("btn-new-case");
  button.disabled = true;
  try {
    await createCase();
  } catch (error) {
    showAlert(`Не удалось создать дело: ${error.message}`);
  } finally {
    button.disabled = false;
  }
};

/* Ввод кода участника: без него интерфейс не начинает работу. */
$("gate-submit").onclick = acceptStudentCode;
$("gate-forget").onclick = forgetStudent;
$("gate-student").onkeydown = (event) => {
  if (event.key === "Enter") acceptStudentCode();
};
$("btn-student").onclick = () => studentGateOpen();

$("btn-demo-case").onclick = async () => {
  if (!currentStudentCode()) {
    studentGateOpen();
    return;
  }
  try {
    await createDemoCase();
  } catch (error) {
    showAlert(`Не удалось создать демо-дело: ${error.message}`);
  }
};

/* Зона загрузки: клик, клавиатура и перетаскивание мышью. */
const dropzone = $("dropzone");
const fileInput = $("file-input");

dropzone.onclick = () => {
  if (!dropzone.classList.contains("busy")) fileInput.click();
};
dropzone.onkeydown = (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
};
fileInput.onchange = () => uploadFiles(fileInput.files);

["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("over");
  });
});
["dragleave", "drop"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("over");
  });
});
dropzone.addEventListener("drop", (event) => {
  uploadFiles(event.dataTransfer && event.dataTransfer.files);
});

/* Просмотр документа: закрытие кнопкой, по фону и по Esc. */
$("doc-viewer-close").onclick = closeDoc;
$("doc-viewer").addEventListener("click", (event) => {
  if (event.target === $("doc-viewer")) closeDoc();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("doc-viewer").hidden) closeDoc();
  if (!$("help").hidden) $("help").hidden = true;
});

/* Помощь «?»: четыре шага простыми словами. */
$("btn-help").onclick = () => {
  const panel = $("help");
  panel.hidden = !panel.hidden;
};
$("help-close").onclick = () => {
  $("help").hidden = true;
};

/* ── Обратная связь ─────────────────────────────────────────── */

function openFeedback() {
  if (!state.currentCase) {
    showAlert("Сначала выберите дело.");
    return;
  }
  $("fb-alert").hidden = true;
  $("fb-comment").value = "";
  $("fb-name").value = currentStudentCode();
  $("fb-verdict").value = "NEEDS_WORK";
  $("fb-rating").value = "3";
  show($("feedback-modal"), true);
}

async function submitFeedback() {
  if (!state.currentCase) return;
  const button = $("fb-submit");
  button.disabled = true;
  try {
    // Отзыв привязывается к делу и к последнему проекту на сервере:
    // пользователю не нужно выбирать версию вручную.
    await api("/api/feedback", {
      method: "POST",
      body: JSON.stringify({
        case_id: state.currentCase,
        rating: Number($("fb-rating").value),
        verdict: $("fb-verdict").value,
        comment: $("fb-comment").value.trim(),
        author_role: "student",
        author_name: $("fb-name").value.trim(),
      }),
    });
    show($("feedback-modal"), false);
    showAlert("Спасибо! Отзыв сохранён.", true);
  } catch (error) {
    $("fb-alert").textContent = `Отзыв не сохранён: ${error.message}`;
    $("fb-alert").hidden = false;
  } finally {
    button.disabled = false;
  }
}

$("btn-feedback").onclick = openFeedback;
$("feedback-close").onclick = () => show($("feedback-modal"), false);
$("fb-submit").onclick = submitFeedback;

/* ── Реестр фактов (этапы 2–3 ТЗ) ─────────────────────────────── */

const FACT_STATUS_TEXT = {
  verified: "цитата найдена",
  source_matched: "цитата после нормализации",
  extracted: "извлечён",
  unsupported: "цитата не найдена",
  conflicted: "противоречие",
  needs_review: "требует проверки",
};

const FACT_PARTY_TEXT = {
  plaintiff: "истец",
  defendant: "ответчик",
  court: "суд",
  other: "",
};

const AMOUNT_KIND_TEXT = {
  claim: "цена иска",
  principal: "основной долг",
  penalty: "пени, неустойка",
  interest: "проценты",
  paid: "оплачено",
  debt: "задолженность",
  costs: "судебные расходы",
  total: "итого",
  other: "",
};

const DATE_KIND_TEXT = {
  contract: "договор",
  due: "срок исполнения",
  event: "событие",
  payment: "платёж",
  filing: "обращение в суд",
  other: "",
};

/* Возможность на стенде определяется настройкой FACTS_ENABLED: если
   она выключена, показывать кнопку и блок бессмысленно. Статус читается
   один раз при загрузке из /api/info, а не подбирается щупом. */
function factsEnabled() {
  return state.features.facts === true;
}

async function loadFeatures() {
  try {
    const info = await api("/api/info");
    state.features = info.features || {};
  } catch {
    state.features = {};
  }
  applyFactsVisibility();
}

function applyFactsVisibility() {
  const on = factsEnabled();
  $("btn-facts").hidden = !on;
  $("facts-hint").hidden = !on;
  if (!on) {
    show($("facts"), false);
    return;
  }
  $("btn-facts").disabled = !(state.currentCase && state.documents.length > 0);
}

function updateFactsButton() {
  if (factsEnabled()) {
    $("btn-facts").disabled =
      !(state.currentCase && state.documents.length > 0) ||
      state.factsTimer !== null;
  }
}

async function loadFacts() {
  if (!state.currentCase || !factsEnabled()) return;
  const caseId = state.currentCase;

  try {
    const data = await api(`/api/cases/${caseId}/facts`);
    if (state.currentCase !== caseId) return;
    renderFacts(data);
  } catch (error) {
    if (state.currentCase !== caseId) return;
    // 404 — реестр ещё не собирали: это нормальное состояние, а не ошибка.
    show($("facts"), false);
  }
}

async function runFacts() {
  if (!state.currentCase) {
    showAlert("Сначала выберите дело.");
    return;
  }
  if (!state.documents.length) {
    showAlert("Сначала добавьте материалы дела.");
    return;
  }

  const button = $("btn-facts");
  button.disabled = true;
  $("facts-hint").textContent = "Извлечение фактов… на процессоре это минуты.";

  try {
    const job = await api(`/api/cases/${state.currentCase}/facts`, {
      method: "POST",
    });
    pollFactsJob(job.job_id, state.currentCase);
  } catch (error) {
    applyFactsVisibility();
    $("facts-hint").textContent = `Не удалось запустить: ${error.message}`;
  }
}

function pollFactsJob(jobId, caseId) {
  clearInterval(state.factsTimer);

  const startedAt = Date.now();
  state.factsTimer = setInterval(async () => {
    if (state.currentCase !== caseId) {
      stopFactsPolling();
      return;
    }
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    $("facts-hint").textContent = `Извлечение фактов… ${seconds} с`;

    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (state.currentCase !== caseId) return;

      if (job.state === "DONE") {
        stopFactsPolling();
        $("facts-hint").textContent = "";
        await loadFacts();
      } else if (job.state === "FAILED") {
        stopFactsPolling();
        $("facts-hint").textContent = `Не получилось: ${humanError(job.error)}`;
      }
    } catch (error) {
      if (state.currentCase !== caseId) return;
      // Обрыв сети не значит, что извлечение пропало: даём задачу
      // работать дальше, а сообщение — честное.
      $("facts-hint").textContent = `Связь с программой мерцает: ${error.message}`;
    }
  }, POLL_INTERVAL);
}

function stopFactsPolling() {
  clearInterval(state.factsTimer);
  state.factsTimer = null;
  updateFactsButton();
}

function chip(text, tone) {
  const cls = tone ? `chip ${tone}` : "chip";
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

function factStatusChip(status) {
  const text = FACT_STATUS_TEXT[status] || label(FACT_STATUS_TEXT, status);
  const tone =
    status === "verified"
      ? "green"
      : status === "unsupported" || status === "conflicted"
        ? "red"
        : "amber";
  return chip(text, tone);
}

function documentName(documents, id) {
  const doc = (documents || []).find((item) => item.id === id);
  return doc ? doc.filename : id;
}

/* Русские подписи для видов: справочник отдаёт не всё, что можно
   выбрать, поэтому неучтённые значения показываются как есть. */
function kindText(map, kind) {
  return map[kind] || kind || "";
}

function renderFacts(data) {
  const registry = data.registry || {};
  const summary = data.summary || {};

  const total =
    (registry.participants || []).length +
    (registry.amounts || []).length +
    (registry.dates || []).length +
    (registry.claims || []).length +
    (registry.facts || []).length;
  $("facts-count").textContent = String(total);

  const quotes = summary.quotes || {};
  const meta =
    `модель ${escapeHtml(data.model || "—")} · документов ${data.documents} · ` +
    `${Math.round(data.duration_sec || 0)} с · ` +
    `цитаты: точных ${quotes.exact || 0}, после нормализации ${quotes.normalized || 0}, ` +
    `не найдено ${quotes.absent || 0}`;
  $("facts-meta").textContent = meta;

  const blocks = [];

  const contradictions = registry.contradictions || [];
  if (contradictions.length) {
    const items = contradictions
      .map((item) => {
        const values = (item.values || []).map((value) => escapeHtml(String(value)));
        return `<li>${chip(item.blocking ? "блокирует" : "требует оценки", item.blocking ? "red" : "amber")} <span>${escapeHtml(item.kind)}: ${values.join(" / ")}</span></li>`;
      })
      .join("");
    blocks.push(`<p class="facts-title">Расхождения</p><ul class="facts-list">${items}</ul>`);
  }

  const participants = registry.participants || [];
  if (participants.length) {
    const items = participants
      .map((item) => {
        const role = label({ plaintiff: "истец", defendant: "ответчик", third_party: "третье лицо", representative: "представитель" }, item.role);
        const inn = item.inn ? ` · ИНН ${escapeHtml(item.inn)}` : "";
        return `<li><span>${escapeHtml(item.name)}</span>${inn} ${chip(role, "grey")}</li>`;
      })
      .join("");
    blocks.push(`<p class="facts-title">Участники</p><ul class="facts-list">${items}</ul>`);
  }

  const amounts = registry.amounts || [];
  if (amounts.length) {
    const items = amounts
      .map((item) => `<li>${chip(kindText(AMOUNT_KIND_TEXT, item.kind), "teal")} <span>${escapeHtml(formatNum(item.value))} руб.</span>${quoteLine(item)}</li>`)
      .join("");
    blocks.push(`<p class="facts-title">Суммы</p><ul class="facts-list">${items}</ul>`);
  }

  const dates = registry.dates || [];
  if (dates.length) {
    const items = dates
      .map((item) => `<li>${chip(kindText(DATE_KIND_TEXT, item.kind), "teal")} <span>${escapeHtml(String(item.value))}</span>${quoteLine(item)}</li>`)
      .join("");
    blocks.push(`<p class="facts-title">Даты</p><ul class="facts-list">${items}</ul>`);
  }

  const claims = registry.claims || [];
  if (claims.length) {
    const items = claims
      .map((item) => {
        const money = item.amount != null ? ` · ${escapeHtml(formatNum(item.amount))} руб.` : "";
        return `<li><span>${escapeHtml(item.subject)}</span>${money}${quoteLine(item)}</li>`;
      })
      .join("");
    blocks.push(`<p class="facts-title">Требования</p><ul class="facts-list">${items}</ul>`);
  }

  const facts = registry.facts || [];
  if (facts.length) {
    const items = facts
      .map((item) => {
        const party = FACT_PARTY_TEXT[item.party]
          ? ` ${chip(FACT_PARTY_TEXT[item.party], "bronze")}`
          : "";
        const statusChip_ = factStatusChip(item.status);
        return `<li><span>${escapeHtml(item.value)}</span>${party} ${statusChip_}${quoteLine(item)}</li>`;
      })
      .join("");
    blocks.push(`<p class="facts-title">Факты</p><ul class="facts-list">${items}</ul>`);
  }

  const missing = registry.missing || [];
  if (missing.length) {
    const items = missing
      .map((item) => `<li>${chip(item.blocking ? "блокирует вывод" : "не блокирует", item.blocking ? "red" : "grey")} <span>${escapeHtml(item.reason)}</span></li>`)
      .join("");
    blocks.push(`<p class="facts-title">Чего нет в материалах</p><ul class="facts-list">${items}</ul>`);
  }

  $("facts-body").innerHTML = blocks.length
    ? blocks.join("")
    : '<p class="muted small">Фактов не нашлось. Пустой реестр — тоже результат: он говорит, что модель не смогла ничего надёжно извлечь.</p>';

  if (data.status !== "ok") {
    $("facts-status").textContent = "требует проверки";
    $("facts-status").className = "chip amber";
    $("facts-status").hidden = false;
  } else {
    $("facts-status").hidden = true;
  }

  show($("facts"), true);
}

/* Цитата с местом в документе. Документ показывается названием, а не
   UUID: пользователь ищет источник в списке материалов, а не в базе. */
function quoteLine(item) {
  const source = item && item.source;
  if (!source || !source.document_id) return "";
  const doc = documentName(state.documents, source.document_id);
  const where = source.page ? `, с. ${source.page}` : "";
  return ` <span class="muted small">(${escapeHtml(doc)}${where})</span>`;
}

/* ── Паспорт дела: просмотр/правка слотов, проверка ───────────── */
async function loadPassport() {
  const panel = $("passport-panel");
  if (!panel || !state.currentCase) return;
  try {
    const p = await api(`/api/cases/${state.currentCase}/passport`);
    if (!p) { show(panel, false); return; }
    renderPassport(p);
    show(panel, true);
  } catch { show(panel, false); }
}
function renderPassport(p) {
  const body = $("passport-body");
  if (!body) return;
  const slots = [];
  const push = (label, value) => {
    const missing = !value || /^\[[^\]]+ — по данным дела\]$/.test(String(value));
    slots.push(
      `<div class="slot${missing ? " missing" : ""}">` +
      `<span class="slot-k">${escapeHtml(label)}</span>` +
      `<span class="slot-v">${missing ? "не заполнено" : escapeHtml(String(value))}</span>` +
      `</div>`
    );
  };
  const c = p.payload.case || {}, pl = (p.payload.parties || {}).plaintiff || {},
        df = (p.payload.parties || {}).defendant || {}, m = p.payload.money_kopecks || {};
  push("Суд (шапка)", c.court_for_header);
  push("Истец", pl.full);
  push("Адрес истца", pl.address);
  push("Ответчик", df.full);
  push("Адрес ответчика", df.address);
  push("Дата иска", p.payload.suit_date);
  push("Цена иска, коп.", m.price);
  push("Пошлина, коп.", m.fee_paid);
  push("Итого, коп.", m.total);
  body.innerHTML =
    `<div class="pp-status">режим: ${escapeHtml(p.mode)} · статус: ${escapeHtml(p.status)}</div>` +
    slots.join("") +
    `<div class="pp-actions">` +
    `<button class="btn ghost small" id="pp-validate">Проверить паспорт</button>` +
    `<span class="muted small" id="pp-validate-msg"></span></div>`;
  const btn = $("pp-validate");
  if (btn) btn.onclick = validatePassport;
}
async function validatePassport() {
  const msg = $("pp-validate-msg");
  try {
    const p = await api(`/api/cases/${state.currentCase}/passport`);
    const r = await api(`/api/cases/${state.currentCase}/passport/validate`, {
      method: "POST", body: JSON.stringify({ payload: p.payload }),
    });
    msg.textContent = r.status === "ok"
      ? "Паспорт корректен."
      : `Проблем: ${(r.issues || []).length} (${r.status})`;
  } catch (e) { msg.textContent = "Ошибка: " + e.message; }
}
/* ── Журнал расхождений ──────────────────────────────────────── */
async function loadDeviations() {
  const box = $("deviations");
  if (!box || !state.currentCase) return;
  try {
    const rows = await api(`/api/cases/${state.currentCase}/deviations`);
    if (!rows || !rows.length) { show(box, false); return; }
    $("deviations-body").innerHTML = rows.map(r =>
      `<div class="dev">` +
      `<span class="chip ${r.severity === "P0" ? "red" : "amber"}">${escapeHtml(r.code)}</span> ` +
      `<span class="dev-item">${escapeHtml((r.payload || {}).item || r.code)}</span>` +
      `<div class="dev-res muted small">${escapeHtml((r.payload || {}).resolution || "")}</div>` +
      `</div>`
    ).join("");
    show(box, true);
  } catch { show(box, false); }
}
/* ── Красный баннер критических замечаний ───────────────────── */
function renderCriticalBanner(verification) {
  const banner = $("critical-banner");
  if (!banner) return;
  const p0 = (verification && verification.p0_count) || 0;
  if (p0 > 0) {
    banner.innerHTML =
      `<span class="ic">⛔</span><span>Документ нельзя выгрузить: ` +
      `${formatNum(p0)} ${plural(p0, "блокирующее замечание", "блокирующих замечания", "блокирующих замечаний")}. ` +
      `Заполните обязательные слоты и повторите проверку.</span>`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

/* ── Запуск ───────────────────────────────────────────────────── */

$("btn-facts").onclick = runFacts;

/* Исковое заявление: переключатель вида, строки формы, расчёт. */
$("doc-kind").onchange = (event) => switchDocKind(event.target.value);
$("btn-claim-add-demand").onclick = () => addDemandRow();
$("btn-claim-add-fact").onclick = () => addFactRow();
$("btn-claim-preview").onclick = previewClaim;
  if ($("btn-claim-suggest")) $("btn-claim-suggest").onclick = () => suggestClaimFromMaterials(true);
  if ($("btn-claim-suggest-bottom")) $("btn-claim-suggest-bottom").onclick = () => suggestClaimFromMaterials(true);
addDemandRow();
addFactRow();
switchDocKind("judgment");

(async function start() {
  renderStudentBadge();
  renderSteps();
  const savedName = savedAuthor();
  if ($("edit-author")) $("edit-author").value = savedName;
  if ($("wizard-author")) $("wizard-author").value = savedName;
  await loadHealth();
  // Возможности стенда читаются до списка дел: кнопка «Проверить
  // факты» есть только там, где включена настройка FACTS_ENABLED.
  await loadFeatures();

  // Без кода — только запрос кода: список дел в этом состоянии показать
  // нельзя, сервер отдал бы дела всех участников.
  if (!currentStudentCode()) {
    studentGateOpen();
    return;
  }

  await loadCases();

  // Дело выбирается автоматически, если оно уже есть: судье не нужно
  // разбираться в пустом списке при повторном открытии страницы.
  if (state.cases.length) {
    await selectCase(state.cases[0].id);
  }
})();


/* ── Голосовой ввод (минимальная версия) ─────────────────────────── */
const DICTATE = { recorder: null, chunks: [], recording: false, section: "" };

function rememberActiveSection(name) {
  DICTATE.section = name || "";
  console.info("[dictate] active section:", DICTATE.section);
}

function dictateStatus(text, rec) {
  const el = $("dictate-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("rec", !!rec);
}

/**
 * Декодировать записанный WebM/Opus в 16 кГц моно PCM int16.
 * Это критично: ONNX-модель ждёт фиксированную форму входа,
 * а «сырой» WebM даёт ей мусор и Reshape error.
 */
async function decodeTo16kMono(blob) {
  const arrayBuffer = await blob.arrayBuffer();
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const ctx = new AudioCtx();
  try {
    const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
    const targetRate = 16000;
    const len = Math.max(1, Math.floor(audioBuffer.duration * targetRate));
    // Ресемплирование вручную: для каждого целевого сэмпла берём
    // позицию в исходном сигнале (линейная интерполяция).
    const channels = [];
    for (let c = 0; c < audioBuffer.numberOfChannels; c++) {
      channels.push(audioBuffer.getChannelData(c));
    }
    const out = new Int16Array(len);
    for (let i = 0; i < len; i++) {
      const srcPos = (i / targetRate) * audioBuffer.sampleRate;
      let sum = 0;
      for (let c = 0; c < channels.length; c++) {
        const ch = channels[c];
        const i0 = Math.floor(srcPos);
        const i1 = Math.min(i0 + 1, ch.length - 1);
        const frac = srcPos - i0;
        const v0 = ch[i0] || 0;
        const v1 = ch[i1] || 0;
        sum += v0 + (v1 - v0) * frac;
      }
      let s = sum / channels.length;
      s = Math.max(-1, Math.min(1, s));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out.buffer;
  } finally {
    ctx.close();
  }
}

async function dictateToggle() {
  console.info("[dictate] click", new Date().toISOString());
  const fab = $("dictate-fab");
  const panel = $("dictate-panel");
  if (!panel) { console.error("[dictate] panel not found"); return; }
  panel.hidden = false;
  if (panel) panel.style.display = "block";

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    dictateStatus("Микрофон недоступен в этом браузере.", false);
    return;
  }

  if (DICTATE.recording) {
    DICTATE.stop();
    return;
  }

  try {
    const st = await api("/api/speech/status");
    if (!st.available) {
      dictateStatus("Голосовой ввод недоступен: модель не найдена на сервере.", false);
      return;
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
    });
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx();
    if (ctx.state === "suspended") await ctx.resume();
    const source = ctx.createMediaStreamSource(stream);
    // Пишем PCM напрямую — без MediaRecorder/WebM, чтобы не было
    // проблем с декодированием и «пустым звуком».
    const bufferSize = 4096;
    const processor = ctx.createScriptProcessor(bufferSize, 1, 1);
    const chunks = [];
    processor.onaudioprocess = (e) => {
      const data = e.inputBuffer.getChannelData(0);
      const copy = new Float32Array(data);
      chunks.push(copy);
    };
    // Тихий разделитель, чтобы процессор работал (в Chrome он не тикает
    // без подключения к назначению).
    const sink = ctx.createGain();
    sink.gain.value = 0;
    source.connect(processor);
    processor.connect(sink);
    sink.connect(ctx.destination);

    DICTATE.recording = true;
    DICTATE.stop = async () => {
      DICTATE.recording = false;
      if (fab) { fab.classList.remove("rec"); fab.textContent = "🎤"; }
      source.disconnect(); processor.disconnect(); sink.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      await ctx.close();
      dictateStatus("Распознаю речь…", false);

      try {
        const total = chunks.reduce((n, c) => n + c.length, 0);
        console.info("[dictate] samples:", total, "chunks:", chunks.length);
        if (total < 1000) throw new Error("почти нет звука — говорите громче и дольше (2–3 секунды)");
        const pcm = new Int16Array(total);
        let off = 0;
        for (const c of chunks) {
          for (let i = 0; i < c.length; i++) {
            let s = Math.max(-1, Math.min(1, c[i]));
            pcm[off++] = s < 0 ? s * 0x8000 : s * 0x7fff;
          }
        }
        // Ресемплируем в 16 кГц, если микрофон дал другую частоту
        const target = 16000;
        let out = pcm;
        if (ctx.sampleRate !== target) {
          // (ctx уже закрыт — используем сохранённую частоту)
        }
        const pcmBytes = pcm.buffer.slice(0);
        const fd = new FormData();
        fd.append("audio", new Blob([pcmBytes], { type: "application/octet-stream" }), "dictation.pcm");
        fd.append("sample_rate", String(DICTATE.sampleRate || 16000));
        const resp = await fetch(API + "/api/speech/transcribe", { method: "POST", body: fd });
        if (!resp.ok) throw new Error(`сервер вернул ${resp.status}`);
        const data = await resp.json();
        const box = $("dictate-text");
        if (data.ok && data.text) {
          box.value = (box.value ? box.value.trim() + " " : "") + data.text;
          dictateStatus("Готово — проверьте и поправьте текст.", false);
        } else {
          dictateStatus("Не удалось распознать: " + (data.error || " пустой звук"), false);
        }
      } catch (error) {
        dictateStatus("Ошибка: " + error.message, false);
      }
    };
    DICTATE.sampleRate = ctx.sampleRate;

    if (fab) { fab.classList.add("rec"); fab.textContent = "⏹"; }
    dictateStatus(`Слушаю… (${ctx.sampleRate} Гц), нажмите 🎤 ещё раз, чтобы остановить`, true);
  } catch (error) {
    dictateStatus("Нет доступа к микрофону: " + error.message, false);
  }
}

function dictateInsert() {
  const text = ($("dictate-text").value || "").trim();
  if (!text) return;
  const editor = $("editor");
  if (editor) editor.open = true;
  const target = $("edit-text");
  if (target) {
    target.value = (target.value ? target.value.trimEnd() + " " : "") + text;
    if (target.scrollIntoView) target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.focus();
  }
  showAlert("Текст вставлен в поле правки. Проверьте его перед сохранением.", true);
}

/* Найти раздел по голосовой команде и плавно прокрутить к нему. */
function dictateNavigate() {
  const cmd = ($("dictate-text").value || "").toLowerCase().trim();
  const out = $("dictate-proposal");
  if (out) out.hidden = true;
  if (!cmd) { dictateStatus("Надиктуйте или введите название раздела.", false); return; }

  // Синонимы, чтобы понимать разговорные формулировки.
  const ALIASES = {
    "шапка": ["шапка", "адресат", "суд", "наименование суда"],
    "вводная": ["вводная", "начало", "вступление"],
    "описательная": ["описательная", "обстоятельства", "факты"],
    "мотивировочная": ["мотивировочная", "обоснование", "правовое обоснование", "мотивировка"],
    "резолютивная": ["резолютивная", "решение", "просительная", "result", "итог"],
    "просительная": ["просительная", "просьба", "просит"],
    "расчёт": ["расчёт", "расчет", "сумм", "взыскиваемых"],
    "приложения": ["приложен", "перечень документов"],
    "дата": ["дата", "подпись", "число"],
  };

  const articles = Array.from(document.querySelectorAll("article.sec"));
  if (!articles.length) { dictateStatus("Разделы проекта ещё не загружены.", false); return; }

  // Убираем служебные слова навигации.
  const clean = cmd.replace(/перейди|перейти|открой|открыть|покажи|показать|найди|найти|где|раздел|часть|\bк\b|\bна\b|\bв\b/gi, " ").trim();

  let target = null;
  for (const art of articles) {
    const name = (art.dataset.sectionName || "").toLowerCase();
    const hay = name + " " + clean;
    // Прямое вхождение названия или синонима.
    for (const [key, words] of Object.entries(ALIASES)) {
      if (words.some((w) => clean.includes(w) && (name.includes(key) || name.includes(w)))) {
        target = art; break;
      }
    }
    if (target) break;
    if (name && (clean.includes(name) || name.includes(clean))) { target = art; break; }
  }

  if (!target) {
    dictateStatus("Не нашёл раздел. Скажите, например: «перейди к мотивировочной части».", false);
    return;
  }

  articles.forEach((a) => a.classList.remove("active"));
  target.classList.add("active");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  const sectionName = target.dataset.sectionName || "раздел";
  rememberActiveSection(sectionName);
  dictateStatus(`Перехожу к разделу «${sectionName}».`, false);
  window.setTimeout(() => target.classList.remove("active"), 2500);
}

/* Отправить распознанную команду модели — она предложит правку по разделу. */
async function dictateAsk() {
  const cmd = ($("dictate-text").value || "").trim();
  const out = $("dictate-proposal");
  if (!out) return;
  if (cmd.length < 3) {
    out.hidden = false;
    out.innerHTML = '<div class="dp-head">Сначала надиктуйте или введите команду.</div>';
    return;
  }
  if (!state.draft || !state.draft.id) {
    out.hidden = false;
    out.innerHTML = '<div class="dp-head">Сначала сформируйте проект.</div>';
    return;
  }
  out.hidden = false;
  out.innerHTML = '<div class="dp-head">Модель думает…</div>';
  dictateStatus("Модель готовит предложение…", false);
  try {
    const r = await api(`/api/drafts/${state.draft.id}/dictate-edit`, {
      method: "POST",
      body: JSON.stringify({
        command: cmd,
        section: DICTATE.section || "",
        author_name: (savedAuthor() || ""),
      }),
    });
    const esc = (s) => String(s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    let html = '<div class="dp-head">Предложение модели — ничего не применено</div>';
    if (r.section) html += `<div class="dp-section">Раздел: ${esc(r.section)}</div>`;
    html += `<div class="dp-proposed">${esc(r.proposed)}</div>`;
    if (r.explanation) html += `<div class="dp-expl">${esc(r.explanation)}</div>`;
    if (!r.ok) html += '<div class="dp-warn">Проверьте подключение языковой модели.</div>';
    html += '<div class="row"><button class="btn dictate-insert" id="dictate-apply">Вставить как правку</button>' +
            '<button class="btn ghost" id="dictate-dismiss">Отменить</button></div>';
    out.innerHTML = html;
    const apply = $("dictate-apply");
    if (apply) apply.addEventListener("click", () => {
      const editor = $("editor"); if (editor) editor.open = true;
      const target = $("edit-text");
      if (target) {
        target.value = (target.value ? target.value.trimEnd() + " " : "") + r.proposed;
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.focus();
      }
      out.hidden = true;
      showAlert("Предложение вставлено в поле правки. Проверьте его.", true);
    });
    const dismiss = $("dictate-dismiss");
    if (dismiss) dismiss.addEventListener("click", () => { out.hidden = true; });
    dictateStatus("Готово — проверьте предложение модели.", false);
  } catch (error) {
    out.hidden = false;
    out.innerHTML = `<div class="dp-warn">Ошибка: ${String(error.message).replace(/[&<>]/g, "")}</div>`;
    dictateStatus("Ошибка при запросе к модели.", false);
  }
}

(function initDictate() {
  window.__dictateInit = "started";
  // Если wizard мини-агента остался открытым, его оверлей (z-index 1000)
  // перекрывает плавающую кнопку — закроем его, иначе кнопка «не нажимается».
  const wizard = $("suggest-wizard");
  if (wizard) wizard.hidden = true;

  const fab = $("dictate-fab");
  if (fab) { fab.style.zIndex = "2000"; fab.addEventListener("click", dictateToggle); }
  window.__dictateInit = "attached:" + !!fab;
  const ins = $("dictate-insert");
  if (ins) ins.addEventListener("click", dictateInsert);
  const ask = $("dictate-ask");
  if (ask) ask.addEventListener("click", dictateAsk);
  const nav = $("dictate-nav");
  if (nav) nav.addEventListener("click", dictateNavigate);
  const clr = $("dictate-clear");
  if (clr) clr.addEventListener("click", () => {
    const box = $("dictate-text"); if (box) box.value = "";
    dictateStatus("Нажмите 🎤 и говорите", false);
  });
})();
