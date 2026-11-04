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

const API = "";              // тот же origin: интерфейс раздаётся бэкендом
const POLL_INTERVAL = 3000;
// Версия сборки интерфейса. Должна совпадать с ?v= в index.html:
// браузер кэширует app.js по этому адресу, и при неизменном ?v он
// продолжает показывать старый файл, даже когда на сервере новый.
const UI_BUILD = "0.6.0";

// Код участника хранится в браузере: студент вводит его один раз, и оба
// его дела (первое и второе) связываются для последующего разбора.
const STUDENT_KEY = "courtdraft_student_code";

function currentStudentCode() {
  try {
    return (localStorage.getItem(STUDENT_KEY) || "").trim();
  } catch {
    return "";
  }
}

function rememberStudentCode(code) {
  try {
    if (code) localStorage.setItem(STUDENT_KEY, code);
  } catch {
    // Приватный режим браузера может запрещать localStorage — не повод
    // ломать создание дела.
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
  $("student-gate").hidden = false;
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
  loadingCaseId: null,
  loadSeq: 0,
  loadRetries: 0,
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
  // Закрываем просмотр документа: иначе поверх нового дела остаётся
  // открытым документ предыдущего, и выглядит это как «данные смешались».
  show($("doc-viewer"), false);
  $("edit-author").value = "judge";
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
  show($("btn-feedback"), true);
  show($("editor"), false);

  await loadCases();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  await loadDocuments();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  await loadDraft();
  if (seq !== state.loadSeq) return;
  if (state.loadingCaseId !== caseId || state.currentCase !== caseId) return;
  $("btn-generate").disabled = !(state.currentCase && state.documents.length > 0);
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

/* ── Формирование проекта ─────────────────────────────────────── */

async function generate() {
  if (!state.currentCase) {
    showAlert("Сначала выберите дело.");
    return;
  }
  if (!state.documents.length) {
    showAlert("Сначала добавьте материалы дела — без них проект не составить.");
    return;
  }

  const button = $("btn-generate");
  button.disabled = true;
  show($("progress"), true);
  $("progress-fill").style.width = "8%";
  $("progress-fill").classList.add("busy");
  $("progress-text").textContent = "Начинаем формирование проекта…";

  try {
    const job = await api(`/api/cases/${state.currentCase}/draft`, {
      method: "POST",
      body: JSON.stringify({ actor: "judge" }),
    });
    pollJob(job.job_id, state.currentCase);
  } catch (error) {
    stopProgress();
    showAlert(`Не удалось запустить формирование: ${error.message}`);
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
    $("progress-fill").style.width = `${Math.min(92, 8 + ticks * 4)}%`;

    try {
      const job = await api(`/api/jobs/${jobId}`);
      if (caseId && state.currentCase !== caseId) {
        stopProgress();
        return;
      }
      $("progress-text").textContent = describeState(job.state, ticks);

      if (job.state === "DONE") {
        stopProgress();
        $("progress-fill").style.width = "100%";
        showAlert("Проект сформирован и проверен.", true);
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

function describeState(jobState, ticks) {
  const seconds = ticks * (POLL_INTERVAL / 1000);
  if (jobState === "PENDING") return "Задача в очереди…";
  if (jobState === "RUNNING") {
    return `Модель пишет проект… ${seconds} с. На процессоре это 2–3 минуты.`;
  }
  return "Готовим проект…";
}

function stopProgress() {
  clearInterval(state.jobTimer);
  state.jobTimer = null;
  $("progress-fill").classList.remove("busy");
  show($("progress"), false);
  $("btn-generate").disabled = !state.currentCase;
}

/* ── Показ проекта ────────────────────────────────────────────── */

function resetDraftView() {
  $("draft-title").textContent = "Проект не сформирован";
  const statusChip = $("draft-status");
  statusChip.hidden = true;
  statusChip.textContent = "";
  statusChip.className = "chip";
  $("draft-meta").textContent =
    "Нажмите «Сформировать проект». Это займёт две-три минуты.";
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
  $("draft-title").textContent = caseItem
    ? `Проект решения по делу ${caseItem.case_number}`
    : "Проект решения";

  const statusChip = $("draft-status");
  statusChip.hidden = false;
  statusChip.textContent = label(DRAFT_STATUS_TEXT, draft.status);
  const verified = draft.status === "VERIFIED" || draft.status === "ACCEPTED";
  statusChip.className = "chip " + (verified ? "green" : draft.status === "REJECTED" ? "red" : "amber");

  $("draft-meta").textContent =
    `версия ${draft.current_version} · разделов ${(draft.sections || []).length} · ` +
    `источников ${(draft.sources || []).length}`;

  show($("metrics"), true);
  show($("btn-download"), true);
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
  renderSources(draft.sources || []);
  await loadVersions();

  $("edit-text").value = draft.text || "";
  show($("editor"), true);
  renderSteps();
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
    const ownIssues = bySection[section.name] || [];
    const sectionKey = section.key || section.name;
    const ownClaims = claimsBySection[sectionKey] || [];
    if (ownClaims.length) matchedClaims[sectionKey] = true;

    const chips = [];
    if (section.needs_review) chips.push('<span class="chip amber">нужна проверка</span>');
    if (section.is_mock) chips.push('<span class="chip grey">заглушка</span>');
    if (!section.needs_review && !section.is_mock) chips.push('<span class="chip green">готово</span>');

    const article = document.createElement("article");
    article.className = "sec" + (section.is_mock ? " stub" : "");
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
  $("m-sources").textContent = formatNum(sources.length);
  show($("sources"), sources.length > 0);
}

async function loadVersions() {
  const caseId = state.currentCase;
  if (!state.draft || !caseId) return;

  let data;
  try {
    data = await api(`/api/cases/${caseId}/draft/versions`);
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

function downloadDraft() {
  if (!state.draft) return;
  const caseItem = currentCaseObject();
  const number = (caseItem ? caseItem.case_number : "дело").replace(/[^\wА-Яа-я№-]+/g, "_");
  const blob = new Blob([state.draft.text || ""], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `Проект_решения_${number}.txt`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ── Правка ───────────────────────────────────────────────────── */

async function saveEdit() {
  if (!state.draft) return;

  const text = $("edit-text").value.trim();
  if (!text) {
    showAlert("Текст правки пустой.");
    return;
  }

  try {
    const version = await api(`/api/drafts/${state.draft.id}/versions`, {
      method: "POST",
      body: JSON.stringify({
        text,
        author: $("edit-author").value,
        comment: $("edit-comment").value,
      }),
    });

    showAlert(
      `Сохранена версия ${version.version} (автор: ${authorName(version.author)}).`,
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

/* ── Запуск ───────────────────────────────────────────────────── */

(async function start() {
  renderStudentBadge();
  renderSteps();
  await loadHealth();

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
