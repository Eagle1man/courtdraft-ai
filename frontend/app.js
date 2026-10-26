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

const state = {
  cases: [],
  currentCase: null,
  documents: [],
  draft: null,
  jobTimer: null,
};

/* ── Работа с API ─────────────────────────────────────────────── */

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch {
    // Программа не отвечает: это не «HTTP 0», а действие —
    // проверить, запущен ли она на этом компьютере.
    throw new Error("Нет связи с программой. Проверьте, что она запущена.");
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
    $("app-ver").textContent = info.version ? `версия ${info.version}` : "";
    if (info.llm_provider === "mock") {
      $("health").textContent = "демонстрационный режим";
      showDemoBanner();
    } else {
      $("health").textContent = "программа готова";
    }
  } catch {
    $("health").textContent = "нет связи";
    $("health-dot").classList.add("off");
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
  try {
    const data = await api("/api/cases?limit=50");
    state.cases = data.items || [];
  } catch (error) {
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
    if (item.is_synthetic) chips.push('<span class="chip bronze">демо</span>');

    card.innerHTML =
      `<div class="num">${escapeHtml(item.case_number)}</div>` +
      `<div class="court">${escapeHtml(item.court_name)}</div>` +
      `<div class="row">${chips.join("")}</div>`;
    card.onclick = () => selectCase(item.id);
    list.appendChild(card);
  });

  renderSteps();
}

async function selectCase(caseId) {
  state.currentCase = caseId;
  await loadCases();
  await loadDocuments();
  await loadDraft();
  $("btn-generate").disabled = false;
  renderSteps();
}

/* Создание пустого дела: два понятных поля, без технических форм. */
async function createCase(number, court) {
  const created = await api("/api/cases", {
    method: "POST",
    body: JSON.stringify({
      case_number: number,
      court_name: court,
      case_type: "civil",
      category: "",
      is_synthetic: false,
    }),
  });

  showAlert(`Дело ${created.case_number} создано. Теперь добавьте материалы.`, true);
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
  if (!state.currentCase) {
    state.documents = [];
    list.innerHTML = '<li class="muted">Дело не выбрано</li>';
    updateDocsLabel();
    renderSteps();
    return;
  }

  try {
    state.documents = await api(`/api/cases/${state.currentCase}/documents`);
  } catch (error) {
    list.innerHTML = `<li class="muted">${escapeHtml(error.message)}</li>`;
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

  const drop = $("dropzone");
  const title = drop.querySelector(".t");
  const previousTitle = title.textContent;
  const loaded = [];
  const failed = [];

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
        `${API}/api/cases/${state.currentCase}/documents/upload`,
        { method: "POST", body: form }
      );

      if (response.ok) {
        loaded.push(file.name);
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
      failed.push(`Нет связи с программой — файл «${file.name}» не загружен.`);
    }
  }

  drop.classList.remove("busy");
  title.textContent = previousTitle;
  $("file-input").value = "";

  await loadDocuments();

  if (loaded.length && !failed.length) {
    showAlert(
      `Загружено: ${loaded.join(", ")}. Материалы уже доступны проекту.`,
      true
    );
  } else if (loaded.length) {
    showAlert(`Загружено ${loaded.length}. Проблемы: ${failed.join(" ")}`);
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
    const detail = await api(`/api/cases/${state.currentCase}/documents/${docId}`);
    const type = label(DOC_TYPE_TEXT, detail.document_type);
    const hidden = detail.placeholders.length
      ? ` · скрыто значений: ${detail.placeholders.length}`
      : "";
    $("doc-viewer-meta").textContent = `${type} · ${chars(detail.characters)}${hidden}`;
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
    pollJob(job.job_id);
  } catch (error) {
    stopProgress();
    showAlert(`Не удалось запустить формирование: ${error.message}`);
  }
}

function pollJob(jobId) {
  clearInterval(state.jobTimer);

  // Прогресс оценочный: бэкенд не сообщает процент готовности,
  // потому что модель не умеет его сообщать. Честнее показать движение,
  // чем выдумывать точные проценты.
  let ticks = 0;

  state.jobTimer = setInterval(async () => {
    ticks += 1;
    $("progress-fill").style.width = `${Math.min(92, 8 + ticks * 4)}%`;

    try {
      const job = await api(`/api/jobs/${jobId}`);
      $("progress-text").textContent = describeState(job.state, ticks);

      if (job.state === "DONE") {
        stopProgress();
        $("progress-fill").style.width = "100%";
        showAlert("Проект сформирован и проверен.", true);
        await loadDraft();
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
  $("draft-status").hidden = true;
  $("draft-meta").textContent =
    "Нажмите «Сформировать проект». Это займёт две-три минуты.";
  show($("metrics"), false);
  show($("verification"), false);
  show($("sources"), false);
  show($("versions"), false);
  show($("editor"), false);
  show($("btn-download"), false);
  $("sections").innerHTML =
    '<p class="muted">Разделы проекта появятся здесь.</p>';
}

async function loadDraft() {
  if (!state.currentCase) {
    state.draft = null;
    resetDraftView();
    renderSteps();
    return;
  }

  let draft;
  try {
    draft = await api(`/api/cases/${state.currentCase}/draft`);
  } catch (error) {
    showAlert(`Не удалось получить проект: ${error.message}`);
    return;
  }

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
    `версия ${draft.current_version} · разделов ${draft.sections.length} · ` +
    `источников ${draft.sources.length}`;

  show($("metrics"), true);
  show($("btn-download"), true);
  $("m-sections").textContent = formatNum(draft.sections.length);

  renderSections(draft);
  renderVerification(draft.verification);
  renderSources(draft.sources);
  await loadVersions();

  $("edit-text").value = draft.text;
  show($("editor"), true);
  renderSteps();
}

function renderSections(draft) {
  const box = $("sections");
  box.innerHTML = "";

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

  draft.sections.forEach((section, index) => {
    matched[section.name] = true;
    const ownIssues = bySection[section.name] || [];

    const chips = [];
    if (section.needs_review) chips.push('<span class="chip amber">нужна проверка</span>');
    if (section.is_mock) chips.push('<span class="chip grey">заглушка</span>');
    if (!section.needs_review && !section.is_mock) chips.push('<span class="chip green">готово</span>');

    const article = document.createElement("article");
    article.className = "sec" + (section.is_mock ? " stub" : "");
    article.innerHTML =
      `<div class="sh"><span class="no">${index + 1}</span>` +
      `<span class="st">${escapeHtml(section.name)}</span>` +
      `${chips.join("")}</div>` +
      `<div class="body"><div class="txt">${escapeHtml(section.text)}</div>` +
      ownIssues.map(remarkHtml).join("") +
      (section.citations && section.citations.length
        ? `<div class="cites">Источники: ${escapeHtml(section.citations.join(", "))}</div>`
        : `<div class="cites">Источник не указан — раздел требует проверки.</div>`) +
      `</div>`;

    box.appendChild(article);
  });

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

  if (draft.sections.some((s) => (s.text || "").includes(NEEDS_REVIEW))) {
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
  if (!state.draft) return;

  let data;
  try {
    data = await api(`/api/cases/${state.currentCase}/draft/versions`);
  } catch {
    show($("versions"), false);
    return;
  }

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

/* ── Связка элементов ─────────────────────────────────────────── */

$("btn-generate").onclick = generate;
$("btn-save-edit").onclick = saveEdit;
$("btn-download").onclick = downloadDraft;

/* Создание дела: простая форма из двух полей. */
$("btn-new-case").onclick = () => {
  const form = $("case-form");
  form.hidden = !form.hidden;
  if (!form.hidden) $("case-number").focus();
};

$("case-form").onsubmit = async (event) => {
  event.preventDefault();
  const number = $("case-number").value.trim();
  const court = $("case-court").value.trim();
  if (!number || !court) {
    showAlert("Заполните номер дела и то, кто его рассматривает.");
    return;
  }
  try {
    await createCase(number, court);
    $("case-form").reset();
    $("case-form").hidden = true;
  } catch (error) {
    showAlert(`Не удалось создать дело: ${error.message}`);
  }
};

$("case-form-cancel").onclick = () => {
  $("case-form").hidden = true;
};

$("btn-demo-case").onclick = async () => {
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

/* ── Запуск ───────────────────────────────────────────────────── */

(async function start() {
  renderSteps();
  await loadHealth();
  await loadCases();

  // Дело выбирается автоматически, если оно уже есть: судье не нужно
  // разбираться в пустом списке при повторном открытии страницы.
  if (state.cases.length) {
    await selectCase(state.cases[0].id);
  }
})();
