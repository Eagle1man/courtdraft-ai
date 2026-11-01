(function () {
  "use strict";

  var HOURS = [
    "08:20–09:50",
    "10:00–11:30",
    "11:40–13:10",
    "13:30–15:00",
    "15:10–16:40",
    "17:00–18:30",
    "18:40–20:10",
    "20:20–21:50"
  ];
  var WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  var MONTHS = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
  ];

  var state = {
    mode: "public",
    tab: "overview",
    faculty: "",
    group: "",
    week: "",
    filters: { group: "", teacher: "", room: "", from: "", to: "" },
    selected: null,
    journal: []
  };

  var DATA = null;
  var ORIGINAL = null;

  function $(id) { return document.getElementById(id); }

  function clone(value) { return JSON.parse(JSON.stringify(value)); }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(message) {
    var box = $("toast");
    box.textContent = message;
    box.classList.add("is-visible");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { box.classList.remove("is-visible"); }, 3200);
  }

  function isoToday() { return new Date().toISOString().slice(0, 10); }

  function parseIso(text) {
    var parts = String(text).split("-");
    return new Date(+parts[0], +parts[1] - 1, +parts[2]);
  }

  function siteDate(iso) {
    var d = parseIso(iso);
    return String(d.getDate()).padStart(2, "0") + "-" +
      String(d.getMonth() + 1).padStart(2, "0") + "-" + d.getFullYear();
  }

  function isoWeek(iso) {
    var date = parseIso(iso);
    var target = new Date(date.getTime());
    var day = (date.getDay() + 6) % 7;
    target.setDate(target.getDate() - day + 3);
    var first = new Date(target.getFullYear(), 0, 4);
    var firstDay = (first.getDay() + 6) % 7;
    first.setDate(first.getDate() - firstDay + 3);
    var week = 1 + Math.round((target - first) / 604800000);
    return { year: target.getFullYear(), week: week };
  }

  function weekMeta(iso) {
    var date = parseIso(iso);
    var day = (date.getDay() + 6) % 7;
    var monday = new Date(date.getTime());
    monday.setDate(date.getDate() - day);
    var today = new Date();
    var days = [];
    for (var i = 0; i < 7; i += 1) {
      var current = new Date(monday.getTime());
      current.setDate(monday.getDate() + i);
      var same = current.toDateString() === today.toDateString();
      days.push(same ? "Сегодня" : current.getDate() + " " + MONTHS[current.getMonth()] + " " + current.getFullYear() + " г.");
    }
    var sunday = new Date(monday.getTime());
    sunday.setDate(monday.getDate() + 6);
    return {
      per: fmtShort(monday) + " - " + fmtShort(sunday),
      today: today.getDate() + " " + MONTHS[today.getMonth()] + " " + today.getFullYear() + " г.",
      days: days
    };
  }

  function fmtShort(date) {
    return String(date.getDate()).padStart(2, "0") + "." +
      String(date.getMonth() + 1).padStart(2, "0") + "." + date.getFullYear();
  }

  function weekLabel(group, week) {
    var meta = DATA.schedules[group][1][week][0];
    return "Неделя " + week + " · " + meta.per;
  }

  function weeksOf(group) {
    return Object.keys(DATA.schedules[group][1]).sort(function (a, b) { return +a - +b; });
  }

  function fillSelect(select, items, value, label) {
    select.innerHTML = "";
    items.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item;
      option.textContent = label ? label(item) : item;
      if (item === value) { option.selected = true; }
      select.appendChild(option);
    });
  }

  // ══ Публичная страница ══════════════════════════════════════

  function syncPublicSelectors() {
    var faculties = Object.keys(DATA.groups);
    if (faculties.indexOf(state.faculty) < 0) { state.faculty = faculties[0] || ""; }
    fillSelect($("pub-faculty"), faculties, state.faculty);

    var groups = Object.keys(DATA.groups[state.faculty] || {});
    if (groups.indexOf(state.group) < 0) { state.group = groups[0] || ""; }
    fillSelect($("pub-group"), groups, state.group);

    var weeks = state.group ? weeksOf(state.group) : [];
    if (weeks.indexOf(state.week) < 0) { state.week = weeks.length ? weeks[0] : ""; }
    fillSelect($("pub-week"), weeks, state.week, function (week) {
      return weekLabel(state.group, week);
    });

    renderPublicGrid();
  }

  function renderPublicGrid() {
    var table = $("pub-grid");
    table.innerHTML = "";
    if (!state.group || !state.week) { return; }

    var data = DATA.schedules[state.group][1][state.week];
    if (!data) { return; }
    var meta = data[0];
    var grid = data[1];
    var days = [];
    for (var d = 1; d <= 7; d += 1) {
      if (d === 7 && !(grid["7"] && Object.keys(grid["7"]).length)) { continue; }
      days.push(d);
    }

    var caption = document.createElement("caption");
    caption.textContent = "Расписание группы " + state.group + " — " + meta.per;
    table.appendChild(caption);

    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    var corner = document.createElement("th");
    corner.scope = "col";
    corner.textContent = "Время";
    headRow.appendChild(corner);
    days.forEach(function (day, index) {
      var th = document.createElement("th");
      th.scope = "col";
      th.innerHTML = WEEKDAYS[day - 1] + "<small>" + escapeHtml(meta.days[index] || "") + "</small>";
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    for (var pair = 1; pair <= HOURS.length; pair += 1) {
      var row = document.createElement("tr");
      var time = document.createElement("td");
      time.textContent = HOURS[pair - 1];
      row.appendChild(time);
      days.forEach(function (day) {
        var cell = document.createElement("td");
        var items = (grid[String(day)] || {})[String(pair)] || [];
        if (items.length) {
          cell.className = "has-lesson";
          items.forEach(function (lesson) {
            var block = document.createElement("div");
            block.className = "lesson";
            var badge = lesson.SUBG && lesson.SUBG !== "0"
              ? ' <span class="badge">п' + escapeHtml(lesson.SUBG) + "</span>" : "";
            block.innerHTML =
              '<div class="lesson__subject">' + escapeHtml(lesson.SUBJECT) + badge + "</div>" +
              '<div class="lesson__meta">' + escapeHtml(lesson.SUBJ_TYPE) + " · " +
              escapeHtml(lesson.NAME) + "</div>" +
              (lesson.AUD ? '<div class="lesson__room">ауд. ' + escapeHtml(lesson.AUD) + "</div>" : "");
            cell.appendChild(block);
          });
        }
        row.appendChild(cell);
      });
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    $("public-scope").textContent =
      DATA.school + " · " + DATA.scope + " · групп: " + countGroups() +
      " · занятий в выборке: " + DATA.lessons.length;
  }

  function countGroups() {
    return Object.keys(DATA.groups).reduce(function (sum, faculty) {
      return sum + Object.keys(DATA.groups[faculty]).length;
    }, 0);
  }

  function shareLink() {
    var params = new URLSearchParams();
    params.set("faculty", state.faculty);
    params.set("group", state.group);
    params.set("week", state.week);
    var url = location.origin + location.pathname + "#public?" + params.toString();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(
        function () { toast("Ссылка скопирована"); },
        function () { window.prompt("Ссылка на группу", url); }
      );
    } else {
      window.prompt("Ссылка на группу", url);
    }
    history.replaceState(null, "", "#public?" + params.toString());
  }

  // ══ Панель: обзор ═══════════════════════════════════════════

  function renderOverview() {
    $("admin-scope").textContent =
      DATA.school + " · выборка: " + countGroups() + " групп, " + DATA.lessons.length + " занятий";
    $("stat-groups").textContent = countGroups();
    $("stat-lessons").textContent = DATA.lessons.length;
    var teachers = {};
    var rooms = {};
    DATA.lessons.forEach(function (lesson) {
      teachers[lesson.teacher] = true;
      if (lesson.room) { rooms[lesson.room] = true; }
    });
    $("stat-teachers").textContent = Object.keys(teachers).length;
    $("stat-rooms").textContent = Object.keys(rooms).length;

    var table = $("runs-table");
    table.innerHTML =
      "<caption>Результаты реального прогона FET v3 (не демо-данные)</caption>" +
      "<thead><tr><th>Файл</th><th>Активностей</th><th>Конфликтов</th><th>Схема</th><th>fet-cl</th><th>Мягких</th></tr></thead>";
    var body = document.createElement("tbody");
    (DATA.runs || []).forEach(function (run) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(run.name) + "</td>" +
        "<td>" + run.activities + "</td>" +
        "<td>" + run.conflicts + "</td>" +
        "<td>" + (run.schema_ok ? '<span class="badge badge--ok">OK</span>' : '<span class="badge badge--demo">ошибки</span>') + "</td>" +
        "<td>" + (run.fet_ok ? '<span class="badge badge--ok">OK</span>' : '<span class="badge badge--demo">ошибки</span>') + "</td>" +
        "<td>" + (run.broken_soft == null ? "—" : run.broken_soft) + "</td>";
      body.appendChild(tr);
    });
    table.appendChild(body);
    $("runs-note").textContent = DATA.runs && DATA.runs.length
      ? "Числа взяты из последней сборки: 7 файлов .fet прошли проверку fet-cl."
      : "Результаты прогона не приложены к демо-выборке.";
  }

  // ══ Панель: занятия ═════════════════════════════════════════

  function filteredLessons() {
    var filters = state.filters;
    var from = filters.from || "";
    var to = filters.to || "";
    return DATA.lessons.filter(function (lesson) {
      if (filters.group && lesson.group.toLowerCase().indexOf(filters.group.toLowerCase()) < 0) { return false; }
      if (filters.teacher && lesson.teacher.toLowerCase().indexOf(filters.teacher.toLowerCase()) < 0) { return false; }
      if (filters.room && lesson.room.toLowerCase().indexOf(filters.room.toLowerCase()) < 0) { return false; }
      if (from && lesson.date < from) { return false; }
      if (to && lesson.date > to) { return false; }
      return true;
    });
  }

  function renderLessons() {
    var rows = filteredLessons();
    var limit = 100;
    var table = $("lessons-table");
    table.innerHTML =
      "<caption>Найдено занятий: " + rows.length + " (показаны первые " + Math.min(limit, rows.length) + ")</caption>" +
      "<thead><tr><th>Дата</th><th>Пара</th><th>Группа</th><th>Предмет</th><th>Вид</th><th>Преподаватель</th><th>Ауд.</th><th></th></tr></thead>";
    var body = document.createElement("tbody");
    rows.slice(0, limit).forEach(function (lesson) {
      var tr = document.createElement("tr");
      var badge = lesson.subgroup && lesson.subgroup !== "0"
        ? ' <span class="badge">п' + escapeHtml(lesson.subgroup) + "</span>" : "";
      tr.innerHTML =
        "<td class=\"num\">" + lesson.date.split("-").reverse().join(".") + "</td>" +
        "<td>" + lesson.pair + "</td>" +
        "<td>" + escapeHtml(lesson.group) + badge + "</td>" +
        "<td>" + escapeHtml(lesson.subject) + "</td>" +
        "<td>" + escapeHtml(lesson.type) + "</td>" +
        "<td>" + escapeHtml(lesson.teacher) + "</td>" +
        "<td>" + escapeHtml(lesson.room) + "</td>" +
        "<td><button type=\"button\" class=\"btn btn--ghost\" data-edit=\"" + lesson.id + "\">править</button></td>";
      body.appendChild(tr);
    });
    table.appendChild(body);
    $("lessons-count").textContent = rows.length
      ? "Фильтр по группе, преподавателю, аудитории и датам. Правка ниже."
      : "Ничего не найдено.";
  }

  function openEditor(id) {
    var lesson = DATA.lessons.find(function (item) { return item.id === id; });
    if (!lesson) { return; }
    state.selected = lesson;
    $("editor").hidden = false;
    $("editor-badge").textContent = lesson.group + " · " + lesson.date;
    $("e-date").value = lesson.date;
    $("e-pair").innerHTML = HOURS.map(function (_, index) {
      var pair = index + 1;
      return '<option value="' + pair + '"' + (pair === lesson.pair ? " selected" : "") + ">Пара " + pair + "</option>";
    }).join("");
    $("e-room").value = lesson.room;
    $("e-teacher").value = lesson.teacher;
    $("e-subject").value = lesson.subject;
    $("diff").hidden = true;
    $("diff").innerHTML = "";
    $("editor").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function collectChanges() {
    if (!state.selected) { return null; }
    var lesson = state.selected;
    var changes = {};
    if ($("e-date").value && $("e-date").value !== lesson.date) { changes.date = $("e-date").value; }
    var pair = Number($("e-pair").value);
    if (pair !== lesson.pair) { changes.pair = pair; }
    if ($("e-room").value.trim() !== lesson.room) { changes.room = $("e-room").value.trim(); }
    if ($("e-teacher").value.trim() !== lesson.teacher) { changes.teacher = $("e-teacher").value.trim(); }
    if ($("e-subject").value.trim() !== lesson.subject) { changes.subject = $("e-subject").value.trim(); }
    return changes;
  }

  function showDiff(changes) {
    var box = $("diff");
    var names = { date: "Дата", pair: "Пара", room: "Аудитория", teacher: "Преподаватель", subject: "Предмет" };
    var rows = Object.keys(changes).map(function (key) {
      var before = key === "date" ? state.selected[key] : state.selected[key];
      var after = changes[key];
      if (key === "date") {
        before = before.split("-").reverse().join(".");
        after = after.split("-").reverse().join(".");
      }
      return "<tr><th>" + names[key] + "</th><td>" + escapeHtml(before) + "</td><td>→</td><td>" + escapeHtml(after) + "</td></tr>";
    });
    box.innerHTML = "<strong>Будет изменено:</strong><table>" + rows.join("") + "</table>";
    box.hidden = false;
  }

  function moveLesson(lesson, changes) {
    var schedule = DATA.schedules[lesson.group][1];
    var oldDate = siteDate(lesson.date);
    var oldSubgroup = lesson.subgroup;
    var removed = null;

    Object.keys(schedule).forEach(function (week) {
      var grid = schedule[week][1];
      Object.keys(grid).forEach(function (day) {
        Object.keys(grid[day]).forEach(function (pair) {
          grid[day][pair] = grid[day][pair].filter(function (item) {
            var match = item.DATE === oldDate && item.LES === String(lesson.pair) &&
              item.SUBJECT === lesson.subject && item.NAME === lesson.teacher &&
              item.AUD === lesson.room && item.SUBG === oldSubgroup;
            if (match && !removed) { removed = item; return false; }
            return true;
          });
          if (!grid[day][pair].length) { delete grid[day][pair]; }
        });
        if (!Object.keys(grid[day]).length) { delete grid[day]; }
      });
      if (!Object.keys(grid).length) { delete schedule[week]; }
    });

    var newDate = changes.date || lesson.date;
    var newPair = changes.pair || lesson.pair;
    var info = isoWeek(newDate);
    var weekKey = String(info.week);
    if (!schedule[weekKey]) { schedule[weekKey] = [weekMeta(newDate), {}]; }
    var newGrid = schedule[weekKey][1];
    var date = parseIso(newDate);
    var dayKey = String((date.getDay() + 6) % 7 + 1);
    newGrid[dayKey] = newGrid[dayKey] || {};
    newGrid[dayKey][String(newPair)] = newGrid[dayKey][String(newPair)] || [];
    newGrid[dayKey][String(newPair)].push({
      GROUP: lesson.group,
      DAY: dayKey,
      LES: String(newPair),
      AUD: changes.room != null ? changes.room : lesson.room,
      WEEK: weekKey,
      SUBG: lesson.subgroup,
      POST: "-",
      NAME: changes.teacher != null ? changes.teacher : lesson.teacher,
      SUBJECT: changes.subject != null ? changes.subject : lesson.subject,
      SUBJ_TYPE: lesson.type,
      DATE: siteDate(newDate),
      CAFEDRA: "-",
      THEME: "-",
      ID: String(lesson.id),
      COURSE: "0",
      FACULTY: "-",
      FORMOBUCH: "-",
      deleted: 0
    });

    if (changes.date) { lesson.date = changes.date; }
    if (changes.pair) { lesson.pair = changes.pair; }
    if (changes.room != null) { lesson.room = changes.room; }
    if (changes.teacher != null) { lesson.teacher = changes.teacher; }
    if (changes.subject != null) { lesson.subject = changes.subject; }
    lesson.week = weekKey;
  }

  function saveEdit() {
    var changes = collectChanges();
    if (!changes) { return; }
    if (!Object.keys(changes).length) { toast("Значения не изменились"); return; }
    showDiff(changes);
    var lesson = state.selected;
    var before = {
      date: lesson.date, pair: lesson.pair, room: lesson.room,
      teacher: lesson.teacher, subject: lesson.subject
    };
    moveLesson(lesson, changes);
    state.journal.unshift({
      time: new Date().toLocaleTimeString("ru-RU"),
      actor: "сотрудник (демо)",
      group: lesson.group,
      subject: lesson.subject,
      before: before,
      changes: changes
    });
    renderLessons();
    renderJournal();
    syncPublicSelectors();
    toast("Правка применена в демо. В системе она попала бы в журнал и БД.");
  }

  function renderJournal() {
    var table = $("journal-table");
    table.innerHTML =
      "<caption>Журнал изменений: " + state.journal.length + " записей</caption>" +
      "<thead><tr><th>Время</th><th>Кто</th><th>Группа</th><th>Что изменено</th></tr></thead>";
    var body = document.createElement("tbody");
    if (!state.journal.length) {
      var empty = document.createElement("tr");
      empty.innerHTML = "<td colspan=\"4\" class=\"muted\">Изменений пока нет. Откройте «Занятия» и нажмите «править».</td>";
      body.appendChild(empty);
    }
    state.journal.forEach(function (entry) {
      var names = { date: "дата", pair: "пара", room: "аудитория", teacher: "преподаватель", subject: "предмет" };
      var text = Object.keys(entry.changes).map(function (key) {
        return names[key] + ": " + entry.changes[key];
      }).join(", ");
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td class=\"num\">" + escapeHtml(entry.time) + "</td>" +
        "<td>" + escapeHtml(entry.actor) + "</td>" +
        "<td>" + escapeHtml(entry.group) + "</td>" +
        "<td>" + escapeHtml(entry.subject) + " — " + escapeHtml(text) + "</td>";
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  // ══ Панель: конфликты и публикация ══════════════════════════

  function renderConflicts() {
    var table = $("conflicts-table");
    table.innerHTML =
      "<caption>Примеры конфликтов (демонстрационные, не данные выгрузки)</caption>" +
      "<thead><tr><th>Вид</th><th>Дата</th><th>Пара</th><th>Кто</th><th>Описание</th></tr></thead>";
    var body = document.createElement("tbody");
    (DATA.conflicts || []).forEach(function (item) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + (item.kind === "student" ? "группа" : "аудитория") + " <span class=\"badge badge--demo\">пример</span></td>" +
        "<td class=\"num\">" + item.date.split("-").reverse().join(".") + "</td>" +
        "<td>" + item.pair + "</td>" +
        "<td>" + escapeHtml(item.entity) + "</td>" +
        "<td>" + escapeHtml(item.detail) + "</td>";
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function renderVersions() {
    var table = $("versions-table");
    table.innerHTML =
      "<caption>Выгрузки для сайта</caption>" +
      "<thead><tr><th>Версия</th><th>Когда</th><th>Период</th><th>Групп</th><th>Занятий</th><th></th></tr></thead>";
    var body = document.createElement("tbody");
    (DATA.versions || []).forEach(function (version) {
      var tr = document.createElement("tr");
      tr.innerHTML =
        "<td>" + escapeHtml(version.version) + (version.current ? ' <span class="badge badge--ok">актуальная</span>' : "") + "</td>" +
        "<td class=\"num\">" + escapeHtml(version.generated.replace("T", " ")) + "</td>" +
        "<td>" + escapeHtml(version.scope) + "</td>" +
        "<td>" + version.groups + "</td>" +
        "<td>" + version.lessons + "</td>" +
        "<td><button type=\"button\" class=\"btn btn--ghost\" data-open-version=\"" + escapeHtml(version.version) + "\">открыть</button></td>";
      body.appendChild(tr);
    });
    table.appendChild(body);
  }

  function log(text) {
    var box = $("joblog");
    box.hidden = false;
    box.textContent += text + "\n";
    box.scrollTop = box.scrollHeight;
  }

  var jobSteps = {
    build: [
      "Сборка .fet: 6 периодов + семестр…",
      "Rasp_11.01: 914 активностей",
      "Semester_2_2026_2027: 7 205 активностей",
      "Готово: 7 файлов, 0 конфликтных слотов"
    ],
    validate: [
      "Запуск fet-cl 7.10.5…",
      "Схема: OK для 7 из 7 файлов",
      "Генерация: 7 из 7 — Generation successful",
      "Нарушено мягких ограничений: 0"
    ],
    export: [
      "Экспорт JSON в формате read-file-groups.php…",
      "groups.json: 5 академий, 173 группы",
      "data.json: 13 недель",
      "Архив site_<версия>.zip готов"
    ]
  };

  function runJob(kind, button) {
    var steps = jobSteps[kind];
    var buttons = document.querySelectorAll("[data-job]");
    buttons.forEach(function (item) { item.disabled = true; });
    $("joblog").textContent = "";
    $("joblog").hidden = false;
    var index = 0;
    function next() {
      if (index >= steps.length) {
        buttons.forEach(function (item) { item.disabled = false; });
        button.disabled = false;
        if (kind === "export") {
          DATA.versions.unshift({
            version: isoToday() + "_" + new Date().toTimeString().slice(0, 5).replace(":", ""),
            generated: new Date().toISOString().slice(0, 19),
            scope: DATA.scope,
            groups: countGroups(),
            lessons: DATA.lessons.length,
            current: true
          });
          DATA.versions.forEach(function (item, position) { item.current = position === 0; });
          renderVersions();
        }
        toast("Демо-операция завершена: " + button.textContent);
        return;
      }
      log("→ " + steps[index]);
      index += 1;
      setTimeout(next, 600);
    }
    next();
  }

  function downloadJson() {
    var payload = {
      note: DATA.note,
      generated: new Date().toISOString(),
      school: DATA.school,
      scope: DATA.scope,
      groups: DATA.groups,
      schedules: DATA.schedules
    };
    var blob = new Blob([JSON.stringify(payload, null, 1)], { type: "application/json" });
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = "schedule-demo.json";
    link.click();
    URL.revokeObjectURL(url);
    toast("Файл сформирован: schedule-demo.json");
  }

  // ══ Переключение режимов ════════════════════════════════════

  function setMode(mode, tab) {
    state.mode = mode;
    if (tab) { state.tab = tab; }
    $("view-public").hidden = mode !== "public";
    $("view-admin").hidden = mode !== "admin";
    $("mode-public").classList.toggle("is-active", mode === "public");
    $("mode-admin").classList.toggle("is-active", mode === "admin");
    document.querySelectorAll(".tab").forEach(function (item) {
      item.classList.toggle("is-active", item.dataset.tab === state.tab);
    });
    ["overview", "lessons", "conflicts", "publish"].forEach(function (name) {
      var panel = $("tab-" + name);
      if (panel) { panel.hidden = name !== state.tab; }
    });
    location.hash = mode === "public" ? "public" : "admin/" + state.tab;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function applyHash() {
    var hash = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (!hash) { return; }
    var parts = hash.split("?");
    var route = parts[0].split("/");
    if (route[0] === "admin") {
      setMode("admin", route[1] || "overview");
      return;
    }
    if (route[0] === "public") {
      if (parts[1]) {
        var params = new URLSearchParams(parts[1]);
        if (params.get("faculty")) { state.faculty = params.get("faculty"); }
        if (params.get("group")) { state.group = params.get("group"); }
        if (params.get("week")) { state.week = params.get("week"); }
        syncPublicSelectors();
      }
      setMode("public");
    }
  }

  // ══ События ═════════════════════════════════════════════════

  function bind() {
    $("mode-public").addEventListener("click", function () { setMode("public"); });
    $("mode-admin").addEventListener("click", function () { setMode("admin", state.tab); });

    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        state.tab = tab.dataset.tab;
        setMode("admin", state.tab);
      });
    });

    $("pub-faculty").addEventListener("change", function (event) {
      state.faculty = event.target.value;
      state.group = "";
      syncPublicSelectors();
    });
    $("pub-group").addEventListener("change", function (event) {
      state.group = event.target.value;
      state.week = "";
      syncPublicSelectors();
    });
    $("pub-week").addEventListener("change", function (event) {
      state.week = event.target.value;
      renderPublicGrid();
    });
    $("share").addEventListener("click", shareLink);
    $("print").addEventListener("click", function () { window.print(); });

    $("apply-filter").addEventListener("click", function () {
      state.filters = {
        group: $("f-group").value.trim(),
        teacher: $("f-teacher").value.trim(),
        room: $("f-room").value.trim(),
        from: $("f-from").value,
        to: $("f-to").value
      };
      renderLessons();
    });
    $("clear-filter").addEventListener("click", function () {
      state.filters = { group: "", teacher: "", room: "", from: "", to: "" };
      ["f-group", "f-teacher", "f-room", "f-from", "f-to"].forEach(function (id) { $(id).value = ""; });
      renderLessons();
    });

    $("lessons-table").addEventListener("click", function (event) {
      var button = event.target.closest("[data-edit]");
      if (button) { openEditor(Number(button.dataset.edit)); }
    });
    $("save-edit").addEventListener("click", saveEdit);
    $("cancel-edit").addEventListener("click", function () {
      $("editor").hidden = true;
      state.selected = null;
      $("diff").hidden = true;
    });
    $("e-date").addEventListener("change", function () {
      var changes = collectChanges();
      if (changes && Object.keys(changes).length) { showDiff(changes); }
    });
    ["e-date", "e-pair", "e-room", "e-teacher", "e-subject"].forEach(function (id) {
      $(id).addEventListener("input", function () {
        var changes = collectChanges();
        if (changes && Object.keys(changes).length) { showDiff(changes); }
      });
    });

    document.querySelectorAll("[data-job]").forEach(function (button) {
      button.addEventListener("click", function () { runJob(button.dataset.job, button); });
    });
    $("download").addEventListener("click", downloadJson);

    $("versions-table").addEventListener("click", function (event) {
      var button = event.target.closest("[data-open-version]");
      if (button) {
        setMode("public");
        toast("Версия " + button.dataset.openVersion + " открыта в режиме просмотра");
      }
    });

    $("reset").addEventListener("click", function () {
      DATA = clone(ORIGINAL);
      state.journal = [];
      state.selected = null;
      $("editor").hidden = true;
      renderAll();
      toast("Демо возвращено к исходной выборке");
    });

    window.addEventListener("hashchange", function () {
      var hash = location.hash.replace(/^#/, "");
      if (hash.startsWith("public") && state.mode !== "public") { setMode("public"); }
      if (hash.startsWith("admin") && state.mode !== "admin") { setMode("admin", state.tab); }
    });
  }

  function renderAll() {
    syncPublicSelectors();
    renderOverview();
    renderLessons();
    renderConflicts();
    renderVersions();
    renderJournal();
  }

  function start() {
    fetch("demo-data.json", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw new Error("demo-data.json: " + response.status); }
        return response.json();
      })
      .then(function (data) {
        DATA = data;
        ORIGINAL = clone(data);
        state.faculty = Object.keys(DATA.groups)[0] || "";
        renderAll();
        bind();
        applyHash();
        setMode(state.mode, state.tab);
      })
      .catch(function (error) {
        var main = document.querySelector("main");
        main.innerHTML = '<div class="notice notice--warn">Не удалось загрузить демо-данные: ' +
          escapeHtml(error.message) + ". Страница должна открываться через веб-сервер, а не с диска.</div>";
      });
  }

  start();
})();
