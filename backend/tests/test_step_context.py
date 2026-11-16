"""BUG-3: шаг плана получает запрос, историю диалога и результаты своих зависимостей.

Задача 3a5f4292: шаг «для каждого этапа из step-1…» отвечал «пришлите список этапов»,
потому что модель видела только свою строку objective. Плюс чтение несуществующего
файла (exists=false) шло как успех.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import aria.core.executor as ex

S1 = "11111111-1111-1111-1111-111111111111"
S2 = "22222222-2222-2222-2222-222222222222"


def _env(plan_json, tmp_path, monkeypatch, *, dialog=()):
    from aria.config import get_settings
    from aria.db import models as m
    from aria.db import repository as repo
    from aria.db.base import init_db, session_scope
    from sqlalchemy import select

    init_db(f"sqlite:///{(tmp_path / 'ctx.db').as_posix()}", create_all=True)
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(tmp_path / "sb"))
    with session_scope() as seed:  # коммитим данные: сессия, которую держит тест, их иначе откатит
        sess = m.Session(title="ctx", current_task_id=None)
        seed.add(sess)
        seed.flush()
        for role, text in dialog:
            repo.append_message(seed, sess, role, text)
        task = m.Task(session_id=sess.id, objective="составь план по TaskSpec")
        seed.add(task)
        seed.flush()
        seed.add(m.TaskPlan(task_id=task.id, plan_json=plan_json))
        task_id = task.id
    cm = session_scope()
    db = cm.__enter__()
    db._test_cm = cm  # держим контекст живым, пока жива сессия
    task = db.get(m.Task, task_id)
    plan = db.execute(select(m.TaskPlan).where(m.TaskPlan.task_id == task_id)).scalar_one()
    return db, task, plan


class _Router:
    """Запоминает сообщения, которые шаг отправил модели."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent: list[list] = []

    async def route_chat(self, cls, messages, **kw):
        self.sent.append(messages)
        return SimpleNamespace(response=SimpleNamespace(text=self.answers.pop(0)))


def _text(messages) -> str:
    return "\n".join(m.content for m in messages)


def test_llm_step_sees_dependency_result_request_and_dialog(tmp_path, monkeypatch):
    plan = [
        {"step_id": S1, "objective": "перечисли этапы", "role": "general", "tool_ref": "llm_task", "depends_on": []},
        {"step_id": S2, "objective": "распиши каждый этап", "role": "general", "tool_ref": "llm_task", "depends_on": [S1]},
    ]
    db, task, p = _env(plan, tmp_path, monkeypatch, dialog=[
        ("user", "давай обсудим TaskSpec"), ("assistant", "TaskSpec это контракт задачи"),
    ])
    router = _Router(["ЭТАП-АЛЬФА, ЭТАП-БЕТА", "готово"])
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=router))
    assert [c["status"] for c in calls] == ["ok", "ok"]
    second = _text(router.sent[1])
    assert "ЭТАП-АЛЬФА, ЭТАП-БЕТА" in second          # результат зависимости
    assert "составь план по TaskSpec" in second        # запрос пользователя
    assert "TaskSpec это контракт задачи" in second    # история диалога
    assert "ЭТАП-АЛЬФА" not in _text(router.sent[0])   # у первого шага результатов ещё нет


def test_file_write_step_gets_context_with_earlier_file_content(tmp_path, monkeypatch):
    seen = {}

    async def _content(router, session, role, objective, context=""):
        seen.setdefault("ctx", []).append(context)
        return "тело файла" if len(seen["ctx"]) == 1 else "второй файл"

    monkeypatch.setattr(ex, "_llm_generate_content", _content)
    plan = [
        {"step_id": S1, "objective": "черновик", "role": "general", "tool_ref": "file_write",
         "path": "a.md", "write_scope": ["a.md"], "depends_on": []},
        {"step_id": S2, "objective": "проверь черновик", "role": "general", "tool_ref": "file_write",
         "path": "b.md", "write_scope": ["b.md"], "depends_on": [S1]},
    ]
    db, task, p = _env(plan, tmp_path, monkeypatch, dialog=[("user", "хочу план")])
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=object()))
    assert [c["status"] for c in calls] == ["ok", "ok"]
    assert "тело файла" not in seen["ctx"][0]
    assert "тело файла" in seen["ctx"][1] and "a.md" in seen["ctx"][1]
    assert "хочу план" in seen["ctx"][1]


def test_failed_dependency_is_reported_to_next_step(tmp_path, monkeypatch):
    """19.4d: зависимый шаг не исполняется, а получает skipped с причиной."""
    plan = [
        {"step_id": S1, "objective": "прочитай contracts/TaskSpec.md", "role": "general",
         "tool_ref": "file_read", "path": "contracts/TaskSpec.md", "depends_on": []},
        {"step_id": S2, "objective": "суммируй", "role": "general", "tool_ref": "llm_task", "depends_on": [S1]},
    ]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    router = _Router(["итог"])
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=router))
    assert calls[0]["status"] == "error" and "not found" in calls[0]["output_json"]["error"]
    assert calls[1]["status"] == "skipped" and "шаг не выполнен" in calls[1]["output_json"]["reason"]
    assert router.sent == [], "провайдер вызван для шага с упавшей зависимостью"


def test_missing_file_is_not_a_successful_read(tmp_path, monkeypatch):
    plan = [{"step_id": S1, "objective": "читай", "role": "general", "tool_ref": "file_read", "path": "nope.md"}]
    db, task, p = _env(plan, tmp_path, monkeypatch)
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=object()))
    assert calls[0]["status"] == "error"


def test_context_is_bounded():
    steps = [{"step_id": "a", "objective": "x"}, {"step_id": "b", "objective": "y"}]
    huge = {"status": "ok", "tool_name": "llm_task", "output_json": {"content": "Я" * 100_000}}
    ctx = ex._compose_step_context("база", steps, 1, [set(), {0}], {0: huge})
    assert len(ctx) <= ex.STEP_CONTEXT_TOTAL_CHARS + 20
    assert "обрезано" in ctx


def test_result_text_by_tool():
    assert "a.md" in ex._step_result_text({"status": "ok", "tool_name": "file_write",
                                           "input_json": {"path": "a.md", "content": "Z"}, "output_json": {}})
    assert ex._step_result_text({"status": "ok", "tool_name": "file_read", "output_json": {"content": "T"}}) == "T"
    assert "boom" in ex._step_result_text({"status": "error", "output_json": {"error": "boom"}})
