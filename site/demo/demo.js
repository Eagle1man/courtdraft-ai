/* Отрисовка демонстрации на сайте.
 *
 * Страница не ходит в API: она читает demo-data.json, собранный
 * скриптом scripts/build_demo_site.py из прогона демо-стенда. Данных
 * в браузере не возникает — всё, что показано, было получено системой
 * ранее и записано в файл.
 *
 * Оформление разделов, пометок и панелей взято из интерфейса продукта
 * (app.css — копия frontend/styles.css). Демо должно выглядеть как
 * продукт: макет на сайте показывает не то, что заказчик увидит
 * после установки.
 */

(function () {
  "use strict";

  var DATA_URL = "demo-data.json";
  var CASES_URL = "cases.json";

  /*
   * Всё, что видит пользователь, — на русском. В данных статусы,
   * уровни замечаний и типы документов приходят английскими кодами
   * (REVIEW_REQUIRED, HIGH, CLAIM), а показываются словами. Неизвестный
   * код показывается как есть: типы документов расширяемы данными, и
   * выдумывать перевод для нового типа нельзя (п. 10 разбора).
   */
  var VERDICT_TEXT = {
    PASS: "Замечаний нет",
    REVIEW_REQUIRED: "Требует проверки специалистом",
    BLOCKED: "Проверка заблокировала показ проекта",
    FAILED: "Проект не может быть показан как готовый"
  };

  var SEVERITY_TEXT = {
    CRITICAL: "критично",
    HIGH: "существенно",
    MEDIUM: "требует внимания",
    LOW: "незначительно"
  };

  var STATUS_TEXT = {
    PASS: "замечаний нет",
    REVIEW_REQUIRED: "требует проверки",
    BLOCKED: "заблокировано"
  };

  var DOC_TYPE_TEXT = {
    CLAIM: "Исковое заявление",
    RESPONSE: "Отзыв",
    CONTRACT: "Договор",
    RECEIPT: "Платёжный документ",
    ACT: "Акт",
    EXPERT_OPINION: "Заключение эксперта",
    EVIDENCE: "Доказательство",
    MOTION: "Ходатайство",
    PROTOCOL: "Протокол",
    OTHER: "Иной документ"
  };

  function label(map, key) {
    return map[key] || key;
  }

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function amount(value) {
    if (typeof value !== "number" || !isFinite(value)) return "";
    return value.toLocaleString("ru-RU").replace(/,/g, " ") + " руб.";
  }

  function show(node, visible) {
    if (node) node.hidden = !visible;
  }

  function renderCase(data) {
    $("case-number").textContent = "Дело № " + data.case_number;
    $("case-court").textContent = data.court_name || "";

    var parties = [];
    if (data.plaintiff) {
      parties.push({ role: "истец", name: data.plaintiff });
    }
    if (data.defendant) {
      parties.push({ role: "ответчик", name: data.defendant });
    }
    if (!parties.length) {
      parties = (data.participants || []).map(function (line) {
        var match = /^(.*?)\s*\((.*?)\)\s*$/.exec(line);
        return match
          ? { role: match[2], name: match[1] }
          : { role: "", name: line };
      });
    }

    $("party-list").innerHTML = parties
      .map(function (party) {
        return (
          '<li><span class="type">' +
          escapeHtml(party.role) +
          "</span>" +
          escapeHtml(party.name) +
          "</li>"
        );
      })
      .join("");
  }

  function renderDocuments(documents) {
    var list = $("doc-list");
    if (!documents.length) {
      list.innerHTML = '<li class="muted">Материалов нет</li>';
      return;
    }
    list.innerHTML = documents
      .map(function (doc) {
        var facts = doc.facts === "0" ? "" : " · фактов: " + doc.facts;
        return (
          "<li>" +
          '<span class="type">' +
          escapeHtml(label(DOC_TYPE_TEXT, doc.document_type) || "Документ") +
          "</span>" +
          escapeHtml(doc.name) +
          escapeHtml(facts) +
          (doc.title ? '<div class="muted small">' + escapeHtml(doc.title) + "</div>" : "") +
          "</li>"
        );
      })
      .join("");
  }

  function renderMeta(data) {
    var sections = data.sections.length;
    var mock = data.sections.filter(function (s) {
      return s.is_mock;
    }).length;
    $("draft-title").textContent = "Проект решения по делу";
    $("draft-meta").textContent =
      "разделов: " +
      sections +
      (mock ? " (заглушек: " + mock + ")" : "") +
      " · источников: " +
      data.sources.length +
      " · проверка: " +
      label(STATUS_TEXT, data.verification.status);
  }

  function renderVerification(verification) {
    var box = $("verification");
    show(box, true);

    var verdict = $("verdict");
    verdict.textContent =
      VERDICT_TEXT[verification.status] || verification.status;
    verdict.className = "verdict " + (verification.status === "PASS" ? "pass" : "review");

    var counts = [];
    counts.push("проверено разделов: " + verification.sections_checked);
    counts.push("замечаний: " + (verification.issues || []).length);
    counts.push("утверждений без источника: " + verification.unsupported_claims);

    var severity = verification.by_severity || {};
    Object.keys(severity).forEach(function (level) {
      counts.push(label(SEVERITY_TEXT, level) + ": " + severity[level]);
    });

    var summary = document.createElement("p");
    summary.className = "muted";
    summary.textContent = counts.join(" · ");
    // Сам список замечаний уходит к разделам текста: замечание
    // читается рядом с абзацем, к которому относится (п. 10 разбора).
    box.appendChild(summary);
  }

  /* Одно замечание для списка. Внутри раздела порядок: уровень, а
   * раздел и так виден вокруг; в общем списке — уровень и раздел.
   * Фрагмент (issue.text) рисуется, если он в записи прогона есть:
   * без него находка остаётся некликабельной, подсказка не врёт. */
  function issueItem(issue, withSection) {
    var where = [label(SEVERITY_TEXT, issue.severity) || issue.severity];
    if (withSection && issue.section) where.push(issue.section);
    return (
      '<li class="' +
      escapeHtml(issue.severity) +
      (issue.text ? " has-quote" : "") +
      '"><span class="where">' +
      escapeHtml(where.join(" · ")) +
      '</span><span class="why">' +
      escapeHtml(issue.reason) +
      "</span>" +
      (issue.text
        ? '<span class="quote">' + escapeHtml(issue.text) + "</span>"
        : "") +
      "</li>"
    );
  }

  /* Находка кликабельна: раскрывает фрагмент и подсвечивает раздел —
   * тот же приём, что в приложении (styles: .issues li.has-quote). */
  document.addEventListener("click", function (event) {
    var item = event.target.closest(".issues > li.has-quote");
    if (!item) return;
    item.classList.toggle("open");
    var section = item.closest(".section");
    if (section) {
      section.classList.add("flash");
      window.setTimeout(function () {
        section.classList.remove("flash");
      }, 900);
    }
  });

  function renderSections(data) {
    var box = $("sections");

    // Замечания группируются по имени раздела: проверка привязывает
    // замечание к разделу текста, и судья читает его рядом с абзацем,
    // а не списком перед решением.
    var issues = (data.verification && data.verification.issues) || [];
    var bySection = {};
    issues.forEach(function (issue) {
      var key = issue.section || "";
      (bySection[key] = bySection[key] || []).push(issue);
    });
    var matched = {};

    box.innerHTML = data.sections
      .map(function (section) {
        matched[section.name] = true;
        var badges = [];
        if (section.needs_review) {
          badges.push('<span class="badge review">требует проверки</span>');
        }
        if (section.is_mock) {
          badges.push('<span class="badge mock">заглушка</span>');
        }
        if (section.citations && section.citations.length) {
          badges.push(
            '<span class="badge source">' +
              section.citations.length +
              " источн.</span>"
          );
        }

        var citations =
          section.citations && section.citations.length
            ? "Источники: " + section.citations.map(escapeHtml).join(", ")
            : "Источник не указан — раздел требует проверки.";

        var sectionIssues = bySection[section.name];
        var issueHtml =
          sectionIssues && sectionIssues.length
            ? '<ul class="issues section-issues">' +
              sectionIssues
                .map(function (issue) {
                  return issueItem(issue, false);
                })
                .join("") +
              "</ul>"
            : "";

        return (
          '<article class="section' +
          (section.needs_review ? " flagged" : "") +
          '"><header><h4>' +
          escapeHtml(section.name) +
          "</h4><div>" +
          badges.join(" ") +
          '</div></header><div class="body">' +
          escapeHtml(section.text) +
          "</div>" +
          issueHtml +
          '<div class="citations">' +
          citations +
          "</div></article>"
        );
      })
      .join("");

    // Замечания, которые проверка не привязала к разделу или привязала
    // к несуществующему, — после текста, а не перед ним.
    var leftover = [];
    Object.keys(bySection).forEach(function (key) {
      if (!matched[key]) leftover = leftover.concat(bySection[key]);
    });
    if (leftover.length) {
      var rest = document.createElement("div");
      rest.className = "unsectioned";
      rest.innerHTML =
        '<p class="muted small">Эти замечания проверка не привязала ' +
        "к конкретному разделу:</p>" +
        '<ul class="issues">' +
        leftover
          .map(function (issue) {
            return issueItem(issue, true);
          })
          .join("") +
        "</ul>";
      box.appendChild(rest);
    }

    var note = document.createElement("p");
    note.className = "muted small";
    note.textContent =
      "Пометка «требует проверки» означает, что подтвердить утверждение " +
      "по материалам дела не удалось.";
    box.appendChild(note);
  }

  function renderSources(sources) {
    if (!sources.length) return;
    show($("sources"), true);
    $("source-list").innerHTML = sources
      .map(function (source) {
        return "<li>" + escapeHtml(source) + "</li>";
      })
      .join("");
  }

  /*
   * Сравнение «проект системы / решение суда» по реальным делам —
   * самое убедительное в проекте, по замечанию рецензента, поэтому
   * оно вынесено на главную страницу демо. Слева — резолютивная часть,
   * собранная системой по материалам дела; справа — как постановил суд.
   * Система решение суда не получала: оно показано только для
   * сравнения, и оба текста обезличены.
   */
  function renderComparison(payload) {
    if (!payload || !payload.cases || !payload.cases.length) return;
    show($("comparison"), true);
    $("comparison-note").textContent =
      "Слева — резолютивная часть, собранная системой по материалам " +
      "реального дела. Справа — как постановил суд по этому же делу. " +
      "Система решение суда не видела: оно приведено только для " +
      "сравнения. Все тексты обезличены — фамилии, инициалы, названия " +
      "организации и номера дел заменены.";
    $("comparison-list").innerHTML = payload.cases
      .map(function (case_, index) {
        var head = "Дело № " + escapeHtml(case_.case_number || case_.slug || "—");
        if (case_.category) head += " · " + escapeHtml(case_.category);
        var claim = amount(case_.claim_amount);
        if (claim) head += " · иск " + claim;
        return (
          '<details class="compare-case"' +
          (index === 0 ? " open" : "") +
          "><summary>" +
          head +
          "</summary>" +
          '<div class="compare-cols">' +
          '<div class="compare-col"><h4>Проект системы</h4>' +
          '<div class="compare-text">' +
          escapeHtml(case_.operative_text || "—") +
          "</div></div>" +
          '<div class="compare-col"><h4>Решение суда</h4>' +
          '<div class="compare-text">' +
          escapeHtml(case_.court_decision_reference || "—") +
          "</div></div>" +
          "</div></details>"
        );
      })
      .join("");
  }

  function fail(message) {
    $("mode").textContent = "демо недоступно";
    $("sections").innerHTML =
      '<p class="muted">Не удалось загрузить данные демонстрации: ' +
      escapeHtml(message) +
      "</p>";
  }

  function start(data) {
    // Модель показывается только когда она отличается от режима: при
    // заглушке это «режим: mock · mock», что выглядит как опечатка.
    var model = data.model && data.model !== data.mode
      ? " · " + data.model
      : "";
    $("mode").textContent = "режим: " + data.mode + model;
    renderCase(data);
    renderDocuments(data.documents || []);
    renderMeta(data);
    renderVerification(data.verification);
    renderSections(data);
    renderSources(data.sources || []);
    show($("how-to-read"), true);

    var claim = amount(data.claim_amount);
    if (claim) {
      var summary = document.createElement("p");
      summary.className = "muted small";
      summary.textContent =
        "Предмет спора: " +
        claim +
        (data.category ? " · " + data.category : "") +
        (data.hearing_date ? " · заседание: " + data.hearing_date : "");
      $("draft-meta").parentNode.appendChild(summary);
    }
  }

  fetch(DATA_URL)
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (data) {
      start(data);
      // Сравнение — вторым запросом: если cases.json не выложили
      // (демо без реальных дел), страница не должна от этого падать —
      // блок просто останется скрытым.
      fetch(CASES_URL)
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(renderComparison)
        .catch(function () {
          /* блок сравнения остаётся скрытым */
        });
    })
    .catch(function (error) {
      fail(error.message);
    });
})();
