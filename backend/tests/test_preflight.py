"""Готовность REAL-режима: проверки не должны врать.

Смысл этих тестов: «не готово» и «готово» — разные ответы, и каждый
отказ должен называть причину. На экране отказ модели выглядит как
пустой раздел, поэтому preflight — единственное место, где причина
отличается от следствия.
"""

from __future__ import annotations

import httpx

from app.config import EmbeddingProviderName, LLMProviderName, Settings
from app.llm.preflight import CheckState, PreflightReport, run_preflight


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "llm_provider": LLMProviderName.OLLAMA,
        "ollama_enabled": True,
        "ollama_model": "qwen3.5:4b",
        "embedding_provider": EmbeddingProviderName.MOCK,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _tags(*names: str, size_gb: float = 4.8) -> dict[str, object]:
    return {"models": [{"name": name, "size": int(size_gb * 1024**3)} for name in names]}


async def _run(
    settings: Settings,
    *,
    tags: dict[str, object] | None = None,
    error: Exception | None = None,
    memory: tuple[float, float] | None = (16.0, 8.0),
    gpu: tuple[float, float] | None = None,
    probe=None,  # noqa: ANN001
) -> PreflightReport:
    async def fetch(url: str) -> dict[str, object]:
        if error is not None:
            raise error
        return tags if tags is not None else _tags("qwen3.5:4b")

    return await run_preflight(
        settings,
        fetch_tags=fetch,
        memory=lambda: memory,
        gpu=lambda: gpu,
        probe=probe,
    )


def _states(report: PreflightReport) -> dict[str, CheckState]:
    return {check.name: check.state for check in report.checks}


def _check(report: PreflightReport, name: str):  # noqa: ANN202
    return next(item for item in report.checks if item.name == name)


async def test_mock_provider_is_not_ready() -> None:
    """MOCK — это не «всё хорошо»: REAL-прогона в этом режиме нет."""
    report = await _run(_settings(llm_provider=LLMProviderName.MOCK))
    assert _states(report)["provider"] is CheckState.FAIL
    assert report.ready is False
    provider = _check(report, "provider")
    assert "mock" in provider.detail.lower()


async def test_ollama_disabled_is_a_failure() -> None:
    report = await _run(_settings(ollama_enabled=False))
    assert _states(report)["ollama_enabled"] is CheckState.FAIL
    assert report.ready is False


async def test_connection_error_is_named_not_guessed() -> None:
    report = await _run(_settings(), error=httpx.ConnectError("refused"))
    states = _states(report)
    assert states["ollama"] is CheckState.FAIL
    assert "model" not in states, "без связи о модели ничего не известно"
    assert "нет связи" in _check(report, "ollama").detail


async def test_server_error_is_distinguished_from_absence() -> None:
    request = httpx.Request("GET", "http://127.0.0.1:11434/api/tags")
    error = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, text="boom", request=request)
    )
    report = await _run(_settings(), error=error)
    check = _check(report, "ollama")
    assert check.state is CheckState.FAIL
    assert "500" in check.detail


async def test_missing_model_lists_what_exists() -> None:
    report = await _run(
        _settings(ollama_model="qwen3.5:7b"),
        tags=_tags("qwen3.5:4b", "llama3.2:3b"),
    )
    check = _check(report, "model")
    assert check.state is CheckState.FAIL
    assert "qwen3.5:4b" in check.hint and "llama3.2:3b" in check.hint


async def test_same_base_but_different_tag_is_not_a_substitute() -> None:
    """«4b» вместо «7b» — другая модель; подставлять её нельзя."""
    report = await _run(
        _settings(ollama_model="qwen3.5:7b"), tags=_tags("qwen3.5:4b")
    )
    assert _states(report)["model"] is CheckState.FAIL


async def test_ready_when_everything_is_in_place() -> None:
    report = await _run(_settings(), memory=(16.0, 8.0), gpu=(6.0, 6.0))
    assert report.ready is True
    assert _states(report)["memory"] is CheckState.OK


async def test_model_bigger_than_ram_and_vram_is_a_failure() -> None:
    report = await _run(_settings(), memory=(8.0, 1.0), gpu=(4.0, 2.0))
    check = _check(report, "memory")
    assert check.state is CheckState.FAIL
    assert "4.8" in check.detail and "1.0" in check.detail
    assert report.ready is False


async def test_model_fits_vram_even_when_ram_is_tight() -> None:
    """Модель на GPU при нехватке RAM — рабочий запуск, а не отказ."""
    report = await _run(_settings(), memory=(8.0, 1.5), gpu=(6.0, 6.0))
    check = _check(report, "memory")
    assert check.state is CheckState.OK
    assert "видеопамять" in check.detail
    assert report.ready is True


async def test_no_headroom_is_not_a_fit() -> None:
    """4.8 ГБ модели в 5.0 ГБ VRAM — «влезает впритык» и падает на контексте."""
    report = await _run(_settings(), memory=(8.0, 1.0), gpu=(6.0, 5.0))
    check = _check(report, "memory")
    assert check.state is CheckState.FAIL
    assert "запасом" in check.detail


async def test_model_goes_to_cpu_when_ram_allows() -> None:
    report = await _run(_settings(), memory=(16.0, 8.0), gpu=(4.0, 1.0))
    check = _check(report, "memory")
    assert check.state is CheckState.WARN
    assert "CPU" in check.detail
    assert report.ready is True


async def test_unknown_memory_is_a_warning_not_a_pass() -> None:
    report = await _run(_settings(), memory=None, gpu=None)
    assert _states(report)["memory"] is CheckState.WARN
    assert report.ready is True


async def test_local_embeddings_warn_but_do_not_block() -> None:
    report = await _run(
        _settings(embedding_provider=EmbeddingProviderName.LOCAL)
    )
    assert _states(report)["embeddings"] is CheckState.WARN
    assert report.ready is True


async def test_probe_ok_means_model_answers_text() -> None:
    async def probe(settings: Settings) -> tuple[bool, str]:
        return True, "ответ 12 символов за 900 мс"

    report = await _run(_settings(), probe=probe)
    assert _states(report)["answer"] is CheckState.OK
    assert report.ready is True


async def test_empty_answer_fails_the_run() -> None:
    """Пустой content — самый частый отказ REAL: модель «ответила» и молчит."""

    async def probe(settings: Settings) -> tuple[bool, str]:
        return False, "модель ответила пустым текстом"

    report = await _run(_settings(), probe=probe)
    assert _states(report)["answer"] is CheckState.FAIL
    assert report.ready is False


def test_report_serialises_states() -> None:
    assert PreflightReport().to_dict() == {"ready": True, "checks": []}
