"""Логи по подсистемам, task_id в каждой строке, без успешных HTTP-запросов.

Главный файл `backend.log` остаётся (его читает страница Logs). Рядом пишутся отдельные файлы,
чтобы разбирать сбой по подсистеме, не листая всё подряд:

  tasks.log   задачи, исполнитель, аудит, инструменты, цикл агента
  llm.log     роутер моделей, пул ключей, замеры этапов, сжатие
  http.log    uvicorn и HTTP-клиенты (успешные запросы не пишутся)
  errors.log  всё уровня WARNING и выше из любой подсистемы
"""
from __future__ import annotations

import contextvars
import logging
import logging.handlers
import os
from pathlib import Path
from typing import Callable

current_task_id: contextvars.ContextVar[str] = contextvars.ContextVar("aria_task_id", default="")

SUBSYSTEM_FILES: dict[str, tuple[str, ...]] = {
    "tasks.log": ("local_agent.tasks", "local_agent.taskrunner", "local_agent.audit", "local_agent.tools",
                  "local_agent.loop", "local_agent.toolhooks", "local_agent.checkpoints", "aria.core", "aria.tools"),
    "llm.log": ("local_agent.llm", "local_agent.router", "local_agent.key_pool", "local_agent.timing",
                "local_agent.compression", "local_agent.runprofile"),
    "http.log": ("uvicorn", "httpx", "httpcore", "aria.web"),
}


def set_task(task_id) -> contextvars.Token:
    """Привязывает дальнейшие строки лога этого потока выполнения к задаче."""
    return current_task_id.set(str(task_id) if task_id else "")


def reset_task(token: contextvars.Token) -> None:
    try:
        current_task_id.reset(token)
    except ValueError:  # токен из другого контекста: просто очищаем
        current_task_id.set("")


_factory_installed = False


def install_record_factory() -> None:
    """Каждой записи добавляет `task_tag` (` [task=3a5f4292]` или пусто)."""
    global _factory_installed
    if _factory_installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old(*args, **kwargs)
        tid = current_task_id.get()
        tag = ""
        if tid:
            short = tid[:8]
            try:
                already = short in record.getMessage()
            except Exception:  # noqa: BLE001
                already = False
            if not already:
                tag = f" [task={short}]"
        record.task_tag = tag
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class TaskTagFilter(logging.Filter):
    """Запись, созданная в обход фабрики (вручную через LogRecord), получает пустой task_tag, а не KeyError."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "task_tag"):
            record.task_tag = ""
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s%(task_tag)s"


class PrefixFilter(logging.Filter):
    def __init__(self, prefixes: tuple[str, ...]):
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.prefixes)


def is_successful_access(record: logging.LogRecord) -> bool:
    """True для строки uvicorn.access с кодом ответа меньше 400."""
    if record.name != "uvicorn.access":
        return False
    args = record.args
    if isinstance(args, tuple) and len(args) == 5:
        try:
            return int(args[4]) < 400
        except (TypeError, ValueError):
            return False
    return False


class QuietAccessFilter(logging.Filter):
    """Успешные HTTP-запросы (коды < 400) не пишутся: UI опрашивает бэкенд каждые 2-3 с и забивал ими лог.
    Ошибки остаются. ARIA_LOG_ACCESS=1 возвращает всё."""

    def filter(self, record: logging.LogRecord) -> bool:
        if os.environ.get("ARIA_LOG_ACCESS") == "1":
            return True
        return not is_successful_access(record)


def install_subsystem_handlers(
    log_dir: Path,
    formatter_factory: Callable[[str], logging.Formatter],
    *,
    logger: logging.Logger | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backups: int = 3,
) -> list[logging.Handler]:
    """Вешает на корневой логгер файлы по подсистемам и errors.log. Повторный вызов не дублирует."""
    root = logger or logging.getLogger()
    log_dir.mkdir(parents=True, exist_ok=True)
    made: list[logging.Handler] = []
    existing = {getattr(h, "_aria_subsystem", None) for h in root.handlers}
    specs: list[tuple[str, logging.Filter | None, int]] = [
        (name, PrefixFilter(prefixes), logging.INFO) for name, prefixes in SUBSYSTEM_FILES.items()
    ]
    specs.append(("errors.log", None, logging.WARNING))
    for name, flt, level in specs:
        if name in existing:
            continue
        handler = logging.handlers.RotatingFileHandler(log_dir / name, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        handler.setFormatter(formatter_factory(LOG_FORMAT))
        handler.setLevel(level)
        handler.addFilter(TaskTagFilter())
        handler.addFilter(QuietAccessFilter())
        if flt is not None:
            handler.addFilter(flt)
        handler._aria_subsystem = name  # type: ignore[attr-defined]
        root.addHandler(handler)
        made.append(handler)
    for uv in ("uvicorn", "uvicorn.access"):  # у них propagate=False: root-хендлеры сами не получат записи
        ul = logging.getLogger(uv)
        for h in made:
            if h not in ul.handlers:
                ul.addHandler(h)
    return made
install_record_factory()
