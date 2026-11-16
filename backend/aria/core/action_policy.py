"""Политика действий (этап 3 ТЗ 0.2.1): auto / approve / deny без вопросов владельцу.

Рельсы не спрашивают пользователя: чтение и работа внутри проекта идут сами,
опасное снаружи — жёсткий deny, отправка/оплата/внешний POST — Approve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aria.tools.validators import classify_shell_command

Action = str  # "allow" | "approve" | "deny" | "defer"


@dataclass(frozen=True)
class Decision:
    action: Action
    code: str = ""
    reason: str = ""
    summary: str = ""


_DENY_SHELL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(?:^|[\s;&|])format(?:\.com)?\s+[a-z]:", re.I), "disk_format"),
    (re.compile(r"(?i)\bmkfs(?:\.\w+)?\b"), "disk_format"),
    (re.compile(r"(?i)\bdiskpart\b"), "disk_format"),
    (re.compile(r"(?i)\breg(?:\.exe)?\s+(?:add|delete|import|load|restore)\b"), "registry"),
    (re.compile(r"(?i)\bregedit\b"), "registry"),
    (re.compile(r"(?i)\bbcdedit\b"), "system_boot"),
    (re.compile(r"(?i)\b(?:shutdown|restart-computer|stop-computer)\b"), "system_power"),
    (re.compile(r"(?i)\b(?:apt|apt-get|dnf|yum|pacman|zypper)\s+(?:-\S+\s+)*install\b"), "system_packages"),
    (re.compile(r"(?i)\b(?:winget|choco(?:latey)?|scoop)\s+install\b"), "system_packages"),
    (re.compile(r"(?i)\bgit\s+push\b[^\n;&|]*(?:--force(?:-with-lease)?\b|\s-f\b)"), "force_push"),
    (
        re.compile(
            r"(?i)(?:^|[\s;&|])(?:rm|del|rmdir|rd|move|mv|copy|cp|remove-item)\b[^\n;&|]*"
            r"(?:\s[a-z]:\\|\s/(?:tmp|etc|usr|var|home|root|bin|sbin|dev|proc|sys|opt|boot)(?:/|\s|$)"
            r"|\s/(?=\s|$)|\s~[\\/])"
        ),
        "outside_project",
    ),
)


SEND_TOOLS = frozenset({"email_send", "telegram_send", "send_message", "publish_post", "webhook_send"})
WRITE_OUTSIDE_PROJECT = frozenset({"write_note", "create_vault_branch", "log_decision", "memory_save"})
PROJECT_WRITE = frozenset({"file_write"})


def _outside_project(path_value: str, sandbox_root: str) -> bool:
    raw = (path_value or "").strip()
    if not raw:
        return False
    p = Path(raw)
    windows_abs = bool(re.match(r"^[A-Za-z]:[\\/]", raw)) or raw.startswith("\\\\")
    if p.is_absolute() or windows_abs or raw.startswith("~"):
        base = Path(sandbox_root or ".").resolve()
        try:
            p.resolve().relative_to(base)
            return False
        except (ValueError, OSError):
            return True
    return ".." in Path(raw).parts


def decide(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    sandbox_root: str,
    untrusted: bool = False,
    shell_classifier=classify_shell_command,
) -> Decision:
    """Действие для инструмента. ``defer`` — решает существующий путь (approvals/allowlist)."""
    args = arguments if isinstance(arguments, dict) else {}

    if tool_name == "shell_execute":
        command = str(args.get("command") or "")
        for pattern, code in _DENY_SHELL:
            if pattern.search(command):
                return Decision("deny", code, _deny_reason(code))
        # Внешний POST/upload и git push — существующий shell-approval (curl/git вне allowlist).
        return Decision("defer")

    if tool_name in ("file_write", "file_read") and args.get("path"):
        if _outside_project(str(args["path"]), sandbox_root):
            return Decision("deny", "outside_project", "Путь вне проекта")
        return Decision("allow")

    if tool_name in SEND_TOOLS:
        text = str(args.get("text") or args.get("content") or args.get("body") or args.get("message") or "")
        return Decision("approve", "send", "Отправка сообщения", _one_line(text))

    if untrusted and tool_name in WRITE_OUTSIDE_PROJECT:
        return Decision("approve", "untrusted_context", "Действие по недоверенному контенту", tool_name)

    return Decision("allow")


def _one_line(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _deny_reason(code: str) -> str:
    return {
        "disk_format": "форматирование диска запрещено",
        "registry": "изменение реестра запрещено",
        "system_boot": "изменение загрузчика запрещено",
        "system_power": "выключение/перезагрузка системы запрещены",
        "system_packages": "установка системных пакетов запрещена",
        "force_push": "git push --force запрещён",
        "outside_project": "запись или удаление вне проекта запрещены",
    }.get(code, "действие запрещено")
