/* Паспорт-ИИ: дополнительный блок в существующем интерфейсе.
   Не заменяет старый блок «Паспорт дела» и не отправляет файлы повторно:
   анализ читает документы, уже загруженные в дело. */

const PP_AI_STATUS_TEXT = {
  SUPPORTED: "подтверждено",
  DERIVED: "рассчитано",
  AMBIGUOUS: "неоднозначно",
  CONFLICT: "противоречие",
  NOT_FOUND: "не найдено",
  NEEDS_REVIEW: "проверить",
  UNSUPPORTED: "нет источника",
};

function ppAIChip(status) {
  const tone =
    status === "SUPPORTED" || status === "DERIVED"
      ? "green"
      : status === "CONFLICT"
        ? "red"
        : status === "NOT_FOUND"
          ? "grey"
          : "amber";
  return `<span class="chip ${tone}">${PP_AI_STATUS_TEXT[status] || status}</span>`;
}

async function ppAIRun() {
  if (!state.currentCase) {
    showAlert("Сначала выберите дело.");
    return;
  }
  const caseId = state.currentCase;
  const button = $("btn-passport");
  button.disabled = true;
  try {
    await api(withStudentCode(`/api/cases/${caseId}/passport-ai/analyze`), {
      method: "POST",
      body: "{}",
    });
    ppAIPoll(caseId, 0);
  } catch (error) {
    showAlert(`Не удалось запустить анализ: ${error.message}`);
    button.disabled = false;
  }
}

async function ppAIPoll(caseId, attempt) {
  if (state.currentCase !== caseId) return;
  if (attempt > 120) {
    showAlert("Анализ идёт дольше ожидаемого. Проверьте статус позже.");
    $("btn-passport").disabled = false;
    return;
  }
  try {
    const status = await api(
      withStudentCode(`/api/cases/${caseId}/passport-ai/status`)
    );
    if (status.stage === "validated" || status.stage === "confirmed") {
      $("btn-passport").disabled = false;
      await ppAIRender(caseId);
      return;
    }
  } catch {
    // Временный обрыв — продолжим опрос, как и основной интерфейс.
  }
  setTimeout(() => ppAIPoll(caseId, attempt + 1), 3000);
}

async function ppAIRender(caseId) {
  if (state.currentCase !== caseId) return;
  const analysis = await api(
    withStudentCode(`/api/cases/${caseId}/passport-ai/analysis`)
  );
  const status = await api(
    withStudentCode(`/api/cases/${caseId}/passport-ai/status`)
  );
  const panel = $("passport");
  const body = $("pp-body");
  panel.hidden = false;
  $("btn-passport").hidden = false;
  $("pp-total").textContent = analysis.fields.length;
  $("pp-filled").textContent = analysis.fields.filter(
    (field) => field.value !== null && field.value !== ""
  ).length;
  $("pp-conflicts").hidden = !analysis.conflicts.length;
  $("pp-conflicts").textContent = `конфликтов: ${analysis.conflicts.length}`;

  let html = "";
  for (const conflict of analysis.conflicts) {
    const variants = (conflict.variants || [])
      .map(
        (variant) =>
          `${escapeHtml(String(variant.value))} (${escapeHtml(
            (variant.documents || []).join(", ")
          )})`
      )
      .join(" против ");
    html += `<div class="remark"><b>Противоречие: ${escapeHtml(
      conflict.role
    )}.</b> ${variants}<div class="muted small">${escapeHtml(
      conflict.note || ""
    )}</div></div>`;
  }

  if (!analysis.fields.length) {
    html += '<p class="pp-empty">Поля появятся после завершения анализа.</p>';
  } else {
    html += "<table class='pp-table'><thead><tr>";
    html += "<th>Поле</th><th>Значение</th><th>Статус</th><th>Источник</th><th>Решение</th>";
    html += "</tr></thead><tbody>";
    for (const field of analysis.fields) {
      const sources = (analysis.facts || []).filter(
        (fact) => field.fact_ids && field.fact_ids.includes(fact.id)
      );
      const sourceHtml = sources.length
        ? sources
            .map(
              (fact) =>
                `<div class="pp-source">«${escapeHtml(
                  (fact.quote || "").slice(0, 140)
                )}» (${escapeHtml(fact.locator.filename)})</div>`
            )
            .join("")
        : '<span class="muted small">источник не привязан</span>';
      const value =
        field.value === null || field.value === ""
          ? ""
          : Array.isArray(field.value)
            ? field.value.join(", ")
            : String(field.value);
      html += `<tr><td>${escapeHtml(field.role)}${
        field.required ? " *" : ""
      }</td>`;
      html += `<td><input data-pp-role="${escapeHtml(
        field.role
      )}" value="${escapeHtml(value)}" placeholder="не заполнено"></td>`;
      html += `<td>${ppAIChip(field.status)}</td><td>${sourceHtml}</td>`;
      html += `<td class="pp-actions-cell">`;
      html += `<button class="btn ghost small" data-pp-act="confirmed" data-role="${escapeHtml(
        field.role
      )}" title="Подтвердить значение">✓</button> `;
      html += `<button class="btn ghost small" data-pp-act="rejected" data-role="${escapeHtml(
        field.role
      )}" title="Отклонить значение">✗</button></td></tr>`;
    }
    html += "</tbody></table>";
  }

  for (const finding of analysis.validation || []) {
    html += `<div class="remark${
      finding.level === "error" ? "" : " a"
    }">${escapeHtml(finding.code)}: ${escapeHtml(finding.message)}</div>`;
  }
  body.innerHTML = html;
  $("btn-pp-confirm").disabled = !status.can_confirm;

  body.querySelectorAll("button[data-pp-act]").forEach((button) => {
    button.onclick = () =>
      ppAISetField(
        caseId,
        button.dataset.role,
        button.dataset.ppAct === "rejected"
          ? null
          : body.querySelector(
              `[data-pp-role="${CSS.escape(button.dataset.role)}"]`
            ).value,
        button.dataset.ppAct
      );
  });
}

async function ppAISetField(caseId, role, value, action) {
  try {
    await api(
      withStudentCode(`/api/cases/${caseId}/passport-ai/fields/${role}`),
      {
        method: "PATCH",
        body: JSON.stringify({ value, action }),
      }
    );
    await ppAIRender(caseId);
  } catch (error) {
    showAlert(`Не удалось сохранить поле: ${error.message}`);
  }
}

async function ppAIConfirm() {
  if (!state.currentCase) return;
  try {
    const result = await api(
      withStudentCode(`/api/cases/${state.currentCase}/passport-ai/confirm`),
      { method: "POST", body: "{}" }
    );
    showAlert(
      result.ok
        ? "Паспорт сохранён. Прежняя версия — в бэкапе."
        : "Паспорт не сохранён.",
      result.ok
    );
    if (result.ok && typeof loadPassport === "function") {
      await loadPassport();
    }
  } catch (error) {
    showAlert(`Паспорт не сохранён: ${error.message}`);
  }
}

$("btn-passport").onclick = ppAIRun;
$("btn-pp-revalidate").onclick = ppAIRun;
$("btn-pp-confirm").onclick = ppAIConfirm;
$("btn-passport").hidden = false;
