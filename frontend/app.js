/* Логика интерфейса.
 *
 * Порядок работы повторяет путь судьи: выбрать дело → увидеть материалы →
 * сформировать проект → прочитать его по разделам → посмотреть, что
 * требует проверки → при необходимости внести правку.
 *
 * Формирование идёт в фоне и занимает минуты, поэтому здесь есть опрос
 * состояния задачи. Пока опрос идёт, кнопки блокируются: иначе пользователь
 * запустит вторую генерацию поверх первой и получит две расходящиеся версии.
 *
 * Обёртка api() всегда бросает исключение с текстом ответа сервера:
 * «дело не найдено» и «база недоступна» требуют разных действий, и
 * проглатывать это сообщение нельзя.
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
  const response = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      // Ответ без JSON: оставляем код статуса, он информативнее,
      // чем выдуманная причина.
    }
    throw new Error(detail);
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
    setTimeout(() => show(box, false), 4000);
  }
}

/**
 * Очистить текст, оставляя пометки о проверке видимыми.
 * Пометка «ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ» не должна тонуть в тексте.
 */
function escapeHtml(text) {
  return (text || "").replace(/[&<>"]/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[ch]));
}

const NEEDS_REVIEW = "ТРЕБУЕТ ПРОВЕРКИ СПЕЦИАЛИСТОМ";

/*
  Коды из данных показываются словами: статусы, уровни замечаний и
  типы документов приходят английскими кодами (REVIEW_REQUIRED, HIGH,
  CLAIM). Неизвестный код показывается как есть — типы документов
  расширяемы данными, и выдумывать перевод нельзя (п. 10 разбора).
*/
const SEVERITY_TEXT = {
  CRITICAL: "критично",
  HIGH: "существенно",
  MEDIUM: "требует внимания",
  LOW: "незначительно",
};

const VERIFY_STATUS_TEXT = {
  PASS: "замечаний нет",
  REVIEW_REQUIRED: "требует проверки",
  BLOCKED: "заблокировано",
};

const DRAFT_STATUS_TEXT = {
  DRAFT: "черновик",
  UNDER_REVIEW: "на проверке",
  VERIFIED: "проверен",
  ACCEPTED: "принят",
  REJECTED: "отклонён",
  REGENERATED: "перегенерирован",
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

/* ── Состояние сервиса ────────────────────────────────────────── */

async function loadHealth() {
  const badge = $("health");
  try {
    const info = await api("/api/info");
    badge.textContent = `версия ${info.version} · режим ${info.llm_provider}`;
    if (info.llm_provider === "mock") {
      showDemoBanner();
    }
  } catch {
    badge.textContent = "сервис недоступен";
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
    list.innerHTML = `<li class="muted">${escapeHtml(error.message)}</li>`;
    return;
  }

  if (!state.cases.length) {
    list.innerHTML = '<li class="muted">Дел пока нет</li>';
    return;
  }

  list.innerHTML = "";
  state.cases.forEach((item) => {
    const li = document.createElement("li");
    li.className = item.id === state.currentCase ? "active" : "";
    li.textContent = `${item.case_number} · ${item.court_name}`;
    li.onclick = () => selectCase(item.id);
    list.appendChild(li);
  });
}

async function selectCase(caseId) {
  state.currentCase = caseId;
  await loadCases();
  await loadDocuments();
  await loadDraft();
  $("btn-generate").disabled = false;
}

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
    const text = await fetch(`api/demo/${encodeURIComponent(file.filename)}`)
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

/* ── Материалы ────────────────────────────────────────────────── */

async function loadDocuments() {
  const list = $("doc-list");
  if (!state.currentCase) {
    list.innerHTML = '<li class="muted">Дело не выбрано</li>';
    return;
  }

  try {
    state.documents = await api(`/api/cases/${state.currentCase}/documents`);
  } catch (error) {
    list.innerHTML = `<li class="muted">${escapeHtml(error.message)}</li>`;
    return;
  }

  if (!state.documents.length) {
    list.innerHTML =
      '<li class="muted">Материалов нет. Без них проект не составить.</li>';
    return;
  }

  list.innerHTML = "";
  state.documents.forEach((doc) => {
    const li = document.createElement("li");
    li.innerHTML =
      `<span class="type">${escapeHtml(label(DOC_TYPE_TEXT, doc.document_type) || doc.document_type || "Документ")}</span>` +
      escapeHtml(doc.filename);
    list.appendChild(li);
  });
}

/* ── Формирование проекта ─────────────────────────────────────── */

async function generate() {
  if (!state.currentCase) return;

  const button = $("btn-generate");
  button.disabled = true;
  show($("progress"), true);
  $("progress-fill").style.width = "8%";
  $("progress-fill").classList.add("busy");
  $("progress-text").textContent = "Формирование проекта…";

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
        showAlert(`Формирование не удалось: ${job.error || "причина неизвестна"}`);
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
  return jobState;
}

function stopProgress() {
  clearInterval(state.jobTimer);
  state.jobTimer = null;
  $("progress-fill").classList.remove("busy");
  show($("progress"), false);
  $("btn-generate").disabled = !state.currentCase;
}

/* ── Показ проекта ────────────────────────────────────────────── */

async function loadDraft() {
  if (!state.currentCase) return;

  let draft;
  try {
    draft = await api(`/api/cases/${state.currentCase}/draft`);
  } catch (error) {
    showAlert(`Не удалось получить проект: ${error.message}`);
    return;
  }

  state.draft = draft;

  if (!draft) {
    $("draft-title").textContent = "Проект не сформирован";
    $("draft-meta").textContent =
      "Нажмите «Сформировать проект». Это займёт две-три минуты.";
    show($("verification"), false);
    show($("sources"), false);
    show($("versions"), false);
    show($("editor"), false);
    $("sections").innerHTML =
      '<p class="muted">Разделы проекта появятся здесь.</p>';
    return;
  }

  $("draft-title").textContent = `Проект решения по делу`;
  $("draft-meta").textContent =
    `версия ${draft.current_version} · статус ${label(DRAFT_STATUS_TEXT, draft.status)} · ` +
    `разделов ${draft.sections.length} · ` +
    `источников ${draft.sources.length}`;

  renderSections(draft);
  renderVerification(draft.verification);
  renderSources(draft.sources);
  await loadVersions();

  $("edit-text").value = draft.text;
  show($("editor"), true);
}

function renderSections(draft) {
  const box = $("sections");
  box.innerHTML = "";

  /*
    Замечания группируются по имени раздела: судья читает замечание
    рядом с абзацем, к которому оно относится, а не списком перед
    текстом (п. 10 разбора).
  */
  const issues = (draft.verification && draft.verification.issues) || [];
  const bySection = {};
  issues.forEach((issue) => {
    const key = issue.section || "";
    (bySection[key] = bySection[key] || []).push(issue);
  });
  const matched = {};

  const issueHtml = (list, withSection) =>
    `<ul class="issues section-issues">` +
    list
      .map((issue) => {
        const where = [label(SEVERITY_TEXT, issue.severity) || issue.severity];
        if (withSection && issue.section) where.push(issue.section);
        // Находка с фрагментом кликабельна: подсказка и разворот
        // добавляются только если text есть — иначе класс обещает
        // раскрытие, которого не на что раскрыть.
        const liClass =
          escapeHtml(issue.severity) + (issue.text ? " has-quote" : "");
        return (
          `<li class="${liClass}">` +
          `<span class="where">${escapeHtml(where.join(" · "))}</span>` +
          `<span class="why">${escapeHtml(issue.reason)}</span>` +
          (issue.text
            ? `<span class="quote">${escapeHtml(issue.text)}</span>`
            : "") +
          `</li>`
        );
      })
      .join("") +
    `</ul>`;

  draft.sections.forEach((section) => {
    matched[section.name] = true;
    const div = document.createElement("article");
    div.className = "section" + (section.needs_review ? " flagged" : "");

    const badges = [];
    if (section.needs_review) {
      badges.push('<span class="badge review">требует проверки</span>');
    }
    if (section.is_mock) {
      badges.push('<span class="badge mock">заглушка</span>');
    }
    if (section.citations && section.citations.length) {
      badges.push(
        `<span class="badge source">${section.citations.length} источн.</span>`
      );
    }

    const ownIssues = bySection[section.name];
    div.innerHTML =
      "<header>" +
      `<h4>${escapeHtml(section.name)}</h4>` +
      `<div>${badges.join(" ")}</div>` +
      "</header>" +
      `<div class="body">${escapeHtml(section.text)}</div>` +
      (ownIssues && ownIssues.length ? issueHtml(ownIssues, false) : "") +
      (section.citations && section.citations.length
        ? `<div class="citations">Источники: ${section.citations
            .map(escapeHtml)
            .join(", ")}</div>`
        : '<div class="citations">Источник не указан — раздел требует проверки.</div>');

    box.appendChild(div);
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
      issueHtml(leftover, true);
    box.appendChild(rest);
  }

  if (draft.sections.some((s) => s.text.includes(NEEDS_REVIEW))) {
    const note = document.createElement("p");
    note.className = "muted small";
    note.textContent =
      "Пометка «требует проверки» означает, что подтвердить утверждение " +
      "по материалам дела не удалось.";
    box.appendChild(note);
  }
}

function renderVerification(verification) {
  if (!verification) {
    show($("verification"), false);
    return;
  }

  const needsReview = verification.status !== "PASS";
  const box = $("verdict");
  box.className = "verdict " + (needsReview ? "review" : "pass");
  box.textContent = needsReview
    ? "Проект требует проверки специалистом"
    : "Замечаний нет, все разделы опираются на материалы";

  const summary = document.createElement("p");
  summary.className = "muted small summary";
  summary.textContent =
    `проверено разделов: ${verification.sections_checked} · ` +
    `замечаний: ${verification.issues.length} · ` +
    `утверждений без источника: ${verification.unsupported_claims}`;

  // Список замечаний рисуется внутри разделов (renderSections);
  // здесь остаётся вердикт и счётчики. Старый счётчик убирается,
  // чтобы повторная загрузка проекта не плодила строки.
  const host = $("verification");
  const previous = host.querySelector("p.summary");
  if (previous) previous.remove();
  host.appendChild(summary);
  show(host, true);
}

function renderSources(sources) {
  const list = $("source-list");
  list.innerHTML = "";
  sources.forEach((source) => {
    const li = document.createElement("li");
    li.textContent = source;
    list.appendChild(li);
  });
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
      `${version.text.length} симв. · ${formatTime(version.created_at)}` +
      (version.comment ? ` · ${version.comment}` : "");
    list.appendChild(li);
  });

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

/* ── Запуск ───────────────────────────────────────────────────── */

$("btn-generate").onclick = generate;
$("btn-save-edit").onclick = saveEdit;
$("btn-new-case").onclick = async () => {
  try {
    await createDemoCase();
  } catch (error) {
    showAlert(`Не удалось создать дело: ${error.message}`);
  }
};

// Находка кликабельна: раскрывает полный фрагмент текста акта, из
// которого она взята, и на девять десятых секунды подсвечивает раздел.
// Список замечаний перерисовывается при каждой загрузке проекта,
// поэтому слушатель вешается один раз на документ, а не на элементы.
document.addEventListener("click", (event) => {
  const item = event.target.closest(".issues > li.has-quote");
  if (!item) return;
  item.classList.toggle("open");
  const section = item.closest(".section");
  if (section) {
    section.classList.add("flash");
    window.setTimeout(() => section.classList.remove("flash"), 900);
  }
});

(async function start() {
  await loadHealth();
  await loadCases();

  // Дело выбирается автоматически, если оно уже есть: судье не нужно
  // разбираться в пустом списке при повторном открытии страницы.
  if (state.cases.length) {
    await selectCase(state.cases[0].id);
  }
})();