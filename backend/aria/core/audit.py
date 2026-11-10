"""core/audit.py — §8.3 audit lifecycle + принцип §4.5 'Success определяется
структурой, а не словами модели'.

Verdict строится в два прохода:
1. Структурная проверка (обязательна, не зависит от LLM): все ли запланированные
   tool_calls завершились успешно, есть ли артефакт, нет ли blocked_policy без
   резолюции.
2. Качественная проверка через auditor role (qa_auditor, provider class
   standard_reasoning) — если провайдер недоступен или бюджет исчерпан,
   verdict деградирует в `unaudited`, а не маскирует отсутствие аудита (§25 DoD п.8).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.core import timing
from aria.db import models as m
from aria.db.base import release_write_lock
from aria.db import repository as repo
from aria.core.criteria_audit import (
    check_criteria,
    failed_criteria,
    merge_statuses,
)

logger = logging.getLogger("local_agent.audit")
from aria.db.enums import AuditVerdict, ToolStatus
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderRouter, ProviderUnavailable


def _structural_check(tool_calls: list[m.ToolCall]) -> tuple[bool, list[str]]:
    # Пустой tool_calls обрабатывается раньше, в run_audit (см. §4.5 note там) —
    # сюда он не долетает.
    missing: list[str] = []

    failed = [c for c in tool_calls if c.status in (ToolStatus.error, ToolStatus.blocked_policy)]
    unresolved_attention = [c for c in tool_calls if c.status == ToolStatus.needs_attention]

    if failed:
        missing.append(f"{len(failed)} tool call(s) завершились с error/blocked_policy: " + ", ".join(c.tool_name for c in failed))
    if unresolved_attention:
        missing.append(f"{len(unresolved_attention)} tool call(s) остались needs_attention без резолюции.")

    return (len(missing) == 0), missing


def _tool_calls_detail(tool_calls: list[m.ToolCall]) -> str:
    """Компактное представление реальных input/output tool calls для qa_auditor —
    без него LLM не может подтвердить, что артефакт соответствует objective."""
    lines: list[str] = []
    for c in tool_calls:
        entry: dict = {"tool": c.tool_name, "status": str(c.status)}
        if c.input_json:
            entry["input"] = c.input_json
        if c.output_json:
            entry["output"] = c.output_json
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines)


_CODE_WRITE_TOOLS = frozenset({"file_write", "file_edit", "apply_patch"})


def _changed_code_paths(tool_calls: list[m.ToolCall], sandbox_root: str) -> list[str]:
    """F-004: heavy-аудит нужен только если задача меняла код/файлы."""
    paths: list[str] = []
    root = Path(sandbox_root or ".")
    for call in tool_calls:
        if call.tool_name not in _CODE_WRITE_TOOLS:
            continue
        data = call.input_json if isinstance(call.input_json, dict) else {}
        raw = data.get("path") or data.get("file_path")
        if not raw or not str(raw).lower().endswith((".py", ".pyw")):
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = root / path
        paths.append(str(path))
    return paths


def _heavy_local_checks(paths: list[str]) -> tuple[list[str], list[str], bool]:
    """(findings, notes, heavy_available). ruff — только если реально установлен.

    Без managed toolchain heavy честно деградирует: syntax/структура остаются,
    ruff/pip-audit помечаются «не установлено», а не выдумывают «проверено».
    """
    findings: list[str] = []
    notes: list[str] = []
    from aria import toolchain

    status = toolchain.probe()
    existing = [p for p in paths if Path(p).exists()]
    if not existing:
        notes.append("changed files were not found on disk: ruff skipped")
        return findings, notes, False
    heavy_available = bool(status["versions"].get("ruff"))
    if heavy_available:
        ruff = toolchain.scripts_dir() / ("ruff.exe" if os.name == "nt" else "ruff")
        command = str(ruff) if ruff.is_file() else "ruff"
        code, out = toolchain.run(
            [command, "check", "--output-format", "concise", "--no-cache", *existing], timeout=60
        )
        if code not in (0, 1):
            notes.append(f"ruff failed: {out[:200]}")
        elif out.strip():
            findings.extend(out.splitlines()[:15])
        else:
            notes.append("ruff: no findings")
    else:
        notes.append("ruff is not installed: heavy checks degraded to structure")
    if not status["versions"].get("pip-audit"):
        notes.append("pip-audit is not installed: dependency audit skipped")
    notes.append("pytest: not run automatically (the project test command is unknown)")
    return findings, notes, heavy_available


def _tier_prefer(router: ProviderRouter, tier: str) -> str | None:
    """Light — Flash-Lite (объём), затем DeepSeek; heavy — DeepSeek (база)."""
    try:
        groups = router.providers_by_class.values()
    except AttributeError:  # тестовые/fake-роутеры без каталога: цепочка класса сама выберет
        return None
    providers = [p for group in groups for p in group]
    deepseek = [
        p.provider_id for p in providers
        if p.provider_id.startswith("deepseek") and "reasoner" not in p.provider_id
    ]
    if tier == "light":
        lite = [
            p.provider_id for p in providers
            if "flash-lite" in str(getattr(p, "model", "")).lower()
        ]
        return (lite or deepseek or [None])[0]
    return (deepseek or [None])[0]


# PR-2: pass-маркер целиком как слово, чтобы "token"/"look" не считались успехом.
_PASS_RE = re.compile(
    r"(?<!\w)(?:ok|pass|passed|success|yes)(?!\w)|(?<!\w)соответств\w*",
    re.IGNORECASE,
)


def _parse_audit_verdict(text: str) -> tuple[AuditVerdict, list[str]]:
    """Парсит ответ qa_auditor в (verdict, issues).

    Основной путь — JSON-обёртка, которую просит система в промпте. Фолбэк —
    keyword-эвристика для моделей, проигнорировавших формат. Неоднозначный
    ответ трактуем консервативно: needs_rework (§25 DoD п.8 — отсутствие
    явного pass не маскируется под успех).
    """
    candidates: list[str] = []
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL):
        candidates.append(block.strip())
    if not candidates:
        candidates.append(text.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        verdict_raw = str(data.get("verdict", "")).strip().lower()
        raw_issues = data.get("issues", data.get("недостатки", []))
        if isinstance(raw_issues, str):
            raw_issues = [raw_issues]
        issues = [str(i).strip() for i in (raw_issues or []) if str(i).strip()]
        if verdict_raw in ("pass", "ok", "yes", "success"):
            # pass с замечаниями — противоречие, трактуем как needs_rework
            return (AuditVerdict.needs_rework, issues) if issues else (AuditVerdict.pass_, [])
        if verdict_raw in ("needs_info", "need_info", "needs_more_info", "info", "more_info"):
            # П.5.1 ТЗ: нехватка данных — это запрос уточнения, а не переделка.
            return AuditVerdict.needs_info, issues
        if verdict_raw in ("fail", "needs_rework", "rework", "no", "not_ok", "error"):
            return AuditVerdict.needs_rework, issues
        break  # JSON распознан, но verdict неизвестен — уходим в эвристику

    low = text.lower()
    fail_markers = (
        "недостат", "ошибк", "не соответств", "проблем", "исправь",
        "не выполн", "rework", "not ok",
    )
    if any(m in low for m in fail_markers):
        lines = [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip(" -*\t")]
        return AuditVerdict.needs_rework, [ln for ln in lines if ln][:5]

    # PR-2: pass-маркер ищется как отдельное слово. Раньше сравнение шло подстрокой,
    # и "ok" внутри "token"/"look"/"book" засчитывался как успех аудита.
    if _PASS_RE.search(text):
        return AuditVerdict.pass_, []

    return AuditVerdict.needs_rework, ["Неоднозначный ответ qa_auditor — требуется ручная проверка."]

def _parse_criteria_statuses(text: str) -> dict[str, dict]:
    """Достаёт из ответа qa_auditor статусы по каждому criterion_id.

    Ожидаемый вид: {"criteria": [{"id": "ac-1", "status": "pass", "note": "..."}]}.
    Неизвестный статус и отсутствующие id просто не попадают в результат: за них
    держится структурная проверка (criteria_audit), а не догадка парсера.
    """
    out: dict[str, dict] = {}
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text or "", flags=re.DOTALL) or [text or ""]
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("criteria")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if not cid or status not in ("pass", "fail"):
                continue
            out[cid] = {"status": status, "note": str(item.get("note") or "")}
    return out


@timing.timed("audit")
async def run_audit(
    db: OrmSession,
    session: m.Session,
    task: m.Task,
    router: ProviderRouter | None,
    level: str = "strict",
    auditor_class: str = "standard_reasoning",
    auditor_prefer: str | None = None,
    criteria: list | None = None,
    assumptions: list[str] | None = None,
) -> m.AuditReport:
    """``level``: ``strict`` — структура + проверка второй моделью (по умолчанию); ``light`` — только
    структурная проверка, без вызова модели; ``off`` — аудит отключён владельцем (``unaudited``)."""
    settings = get_settings()

    if level == "off":
        # AC-4: аудит tool-задач не может быть выключен полностью. Профиль ``off``
        # деградирует до структурной проверки (model=structural-only) и честного
        # статуса unverified, а не маскируется строкой model=off.
        logger.warning("audit profile=off is degraded to structural-only for task %s", task.id)
        level = "light"

    tool_calls = repo.list_tool_calls(db, task.id)

    if not tool_calls:
        # §4.5: без единого tool call структурно нечего подтверждать — это
        # не провал выполнения (модель просто ответила текстом, например в
        # чисто разговорном обмене), а отсутствие предмета для аудита.
        # НЕ инкрементируем audit_attempt_no и не тратим audit_max_attempts
        # на это: иначе N подряд чисто разговорных /start на одном и том же
        # task_id (main.py post_message переиспользует task_id для чата)
        # необратимо загоняют задачу в failed после audit_max_attempts
        # попыток, хотя ничего структурно не сломано — инцидент 2026-07-22.
        report = repo.create_audit_report(
            db,
            session,
            task,
            attempt_no=task.audit_attempt_no,
            auditor_role="qa_auditor",
            auditor_model="structural-only",
            budget_degraded=False,
            verdict=AuditVerdict.unaudited,
            plan_vs_fact={"objective": task.objective, "note": "no tool calls — audit is not run", "audit_tier": "unaudited"},
            tool_success_summary={"total": 0, "ok": 0, "error": 0, "blocked_policy": 0},
            missing_requirements=["Разговорный ход без tool calls — не расходует audit_attempt_no."],
            patch_suggestions=[],
            metrics_compared={},
        )
        return report

    attempt_no = task.audit_attempt_no + 1
    task.audit_attempt_no = attempt_no
    db.flush()

    structural_ok, missing = _structural_check(tool_calls)

    # PR-6: критерии приёмки TaskSpec проверяются по фактам, до модели.
    criteria_status = check_criteria(criteria or [], [
        {
            "id": str(c.id),
            "tool_name": c.tool_name,
            "status": getattr(c.status, "value", str(c.status)),
            "input_json": c.input_json or {},
            "output_json": c.output_json or {},
        }
        for c in tool_calls
    ])
    failed_by_fact = failed_criteria(criteria_status)

    tool_success_summary = {
        "total": len(tool_calls),
        "ok": len([c for c in tool_calls if c.status == ToolStatus.ok]),
        "error": len([c for c in tool_calls if c.status == ToolStatus.error]),
        "blocked_policy": len([c for c in tool_calls if c.status == ToolStatus.blocked_policy]),
    }

    budget_degraded = False
    verdict: AuditVerdict
    patch_suggestions: list[str] = []
    auditor_model = "structural-only"
    audit_tier = "structural"
    same_provider = False

    changed_paths = _changed_code_paths(tool_calls, settings.agent_sandbox_root)
    heavy_findings: list[str] = []
    heavy_notes: list[str] = []
    heavy_available = False
    if changed_paths:
        heavy_findings, heavy_notes, heavy_available = _heavy_local_checks(changed_paths)

    if not structural_ok:
        verdict = AuditVerdict.needs_rework if attempt_no < settings.audit_max_attempts else AuditVerdict.fail_after_max_attempts
        patch_suggestions = [f"Исправить: {reason}" for reason in missing]
    elif failed_by_fact:
        # Критерий провален по фактам: мнение модели тут не требуется и не спрашивается.
        verdict = AuditVerdict.needs_rework if attempt_no < settings.audit_max_attempts else AuditVerdict.fail_after_max_attempts
        auditor_model = "criteria-deterministic"
        patch_suggestions = [
            f"Критерий приёмки {cid} не выполнен: {criteria_status[cid].get('note')}" for cid in failed_by_fact
        ]
    elif heavy_findings:
        # F-004: локальные инструменты (ruff) нашли дефекты — это evidence, модель не нужна.
        audit_tier = "heavy"
        auditor_model = "ruff"
        verdict = AuditVerdict.needs_rework if attempt_no < settings.audit_max_attempts else AuditVerdict.fail_after_max_attempts
        patch_suggestions = [f"ruff: {item}" for item in heavy_findings]
    else:
        audit_tier = "heavy" if changed_paths and heavy_available else "light"
        # структура в порядке — качественный проход через auditor role
        if router is None:
            verdict = AuditVerdict.unaudited
            audit_tier = "unaudited"
            budget_degraded = True
        else:
            try:
                criteria_block = ""
                if criteria:
                    criteria_block = (
                        "\nAcceptance criteria: "
                        + json.dumps([dict(c) if isinstance(c, dict) else c for c in criteria], ensure_ascii=False, default=str)
                        + "\nПроставь статус КАЖДОМУ критерию в поле criteria."
                    )
                from pathlib import Path as _Path

                from aria.core.roles import get_role as _get_role

                _asset = _get_role("qa_auditor").system_prompt_asset
                _prompt_path = _Path(__file__).resolve().parent.parent / _asset
                try:
                    _auditor_prompt = _prompt_path.read_text(encoding="utf-8").strip()
                except OSError:
                    _auditor_prompt = "Ты qa_auditor. Проверяй результат по фактам; нет доказательства — fail."
                messages = [
                    ChatMessage(
                        role="system",
                        content=(
                            _auditor_prompt
                            + "\n\nОтветь строго в формате JSON: "
                            "{\"verdict\": \"pass\" | \"needs_rework\" | \"needs_info\", \"issues\": [\"недостаток 1\", \"...\"], "
                            "\"criteria\": [{\"id\": \"ac-1\", \"status\": \"pass\" | \"fail\", \"note\": \"коротко\"}]}. "
                            "pass — только если результат полностью соответствует objective, все критерии выполнены и замечаний нет."
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            f"Objective: {task.objective}\n"
                            f"Tool calls summary: {tool_success_summary}\n"
                            f"Tool calls detail: {_tool_calls_detail(tool_calls)}"
                            f"{criteria_block}"
                        ),
                    ),
                ]
                release_write_lock(db)  # BUG-5
                prefer = auditor_prefer or _tier_prefer(router, audit_tier)
                with timing.stage("audit_model_call"):  # вторая модель отдельно от структурной проверки
                    result = await router.route_chat(
                        auditor_class, messages, tools=[], allow_degrade=True, db=db, prefer_provider_id=prefer,
                        resilient=True, fallback_classes=("standard_reasoning", "free_tier_reasoning"),
                        reject_empty=True,
                        purpose="audit", session_id=task.session_id, task_id=task.id,
                    )
                auditor_model = result.provider_id
                same_provider = result.provider_id.startswith("deepseek")
                budget_degraded = result.degraded_to_free
                verdict, parsed_issues = _parse_audit_verdict(result.response.text)
                if parsed_issues:
                    patch_suggestions = [f"QA: {issue}" for issue in parsed_issues]
                # PR-6: статусы критериев от модели накладываются на структурные.
                criteria_status = merge_statuses(criteria_status, _parse_criteria_statuses(result.response.text))
                model_failed = failed_criteria(criteria_status)
                if model_failed:
                    verdict = AuditVerdict.needs_rework
                    patch_suggestions += [
                        f"Критерий приёмки {cid} не выполнен: {criteria_status[cid].get('note')}" for cid in model_failed
                    ]
            except ProviderUnavailable:
                verdict = AuditVerdict.unaudited
                budget_degraded = True

    logger.info(
        "audit task=%s attempt=%s verdict=%s tier=%s model=%s",
        str(task.id)[:8], attempt_no, verdict.value, audit_tier, auditor_model,
    )
    report = repo.create_audit_report(
        db,
        session,
        task,
        attempt_no=attempt_no,
        auditor_role="qa_auditor",
        auditor_model=auditor_model,
        budget_degraded=budget_degraded,
        verdict=verdict,
        plan_vs_fact={
            "objective": task.objective,
            "tool_success_summary": tool_success_summary,
            "criteria_status": criteria_status,
            "assumptions": list(assumptions or []),
            "audit_tier": audit_tier,
            "audited_same_provider": same_provider,
            "heavy_checks": {"findings": heavy_findings, "notes": heavy_notes},
        },
        tool_success_summary=tool_success_summary,
        missing_requirements=missing,
        patch_suggestions=patch_suggestions,
        metrics_compared={"criteria": criteria_status},
    )
    # S5: для research-задач проверить цитаты без LLM; сеть — вне write-lock.
    try:
        if str(getattr(task, "role", "")) == "research":
            release_write_lock(db)
            from aria.core.verify_citations import maybe_verify_research_answer

            await maybe_verify_research_answer(db, session, task)
    except Exception:  # noqa: BLE001 — проверка цитат не валит аудит
        logger.warning("citation verification failed", exc_info=True)
    return report
