"""tools/validators.py — §11 инварианты + §14.2 high-risk command policy.

Волна 1 (A13): к high-risk-паттернам добавлена **default-deny** политика shell.
Раньше ``shell_execute`` исполнял любую команду, если она не совпала с чёрным
списком regex — то есть «разрешено всё, что не запрещено». Теперь разрешено
только то, что в allowlist; всё остальное требует подтверждения владельца.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil

from aria.db.enums import IdempotencyClass

# §14.2 — high-risk patterns минимум включают:
HIGH_RISK_PATTERNS: list[re.Pattern] = [
    # rm с рекурсивным флагом в любом написании: -rf, -fr, -Rf, -rfv, --recursive
    re.compile(r"\brm\b[^;&|\n]*?\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?=\s|$)"),
    re.compile(r"\b(?:del|erase)\b[^;&|\n]*\s/[sS]\b", re.IGNORECASE),
    re.compile(r"\b(?:rmdir|rd)\b[^;&|\n]*\s/[sS]\b", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\s+.*\bDROP\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+--force\b"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
    # "любые массовые delete/move в пользовательских каталогах" — эвристика:
    re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+.*(/home|/Users|~)", re.IGNORECASE),
    re.compile(r"\bmv\s+.*\*.*\s+/dev/null\b"),
]

# ── Волна 1 (A13): default-deny allowlist ────────────────────────────────
# Читающие и обычные рабочие команды. Всё, чего здесь нет — только через
# подтверждение владельца. Для части бинарников разрешён лишь безопасный набор
# подкоманд/флагов (SUBCOMMAND_ALLOW, _INLINE_CODE_FLAGS, _FIND_DANGEROUS).
SHELL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ls", "dir", "cat", "type", "pwd", "echo", "head", "tail", "wc", "sort",
        "uniq", "cut", "tr", "rg", "grep", "find", "which", "where", "tree",
        "mkdir", "touch", "cp", "copy", "mv", "move", "rm", "del", "test",
        "python", "python3", "pip", "node", "npm", "git", "pytest",
        "black", "ruff", "mypy", "tsc",
    }
)

# Бинарник → допустимые подкоманды (первый не-флаговый токен ОБЯЗАН быть здесь).
SUBCOMMAND_ALLOW: dict[str, frozenset[str]] = {
    "git": frozenset({"status", "log", "diff", "show", "branch", "add", "commit",
                      "ls-files", "rev-parse", "blame", "remote", "tag"}),
    "pip": frozenset({"list", "show", "freeze", "check"}),
    "npm": frozenset({"test", "run", "ls", "list", "view", "outdated", "audit"}),
}
# Интерпретаторы: inline-код (-c/-e/…) = произвольное исполнение.
_INTERPRETERS = frozenset({"python", "python3", "node"})
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-p", "--eval", "--print", "-"})
_FIND_DANGEROUS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
_DELETERS = frozenset({"rm", "del"})
# Запускальщики кода: python файл.py / pytest (conftest) / npm run|test (scripts из package.json) исполняют то,
# что агент мог сам записать файловым тулом. Без подтверждения разрешены только пробы версии.
_CODE_RUNNERS = frozenset({"python", "python3", "node", "pytest"})
_NPM_CODE_SUBCOMMANDS = frozenset({"test", "run"})
_VERSION_PROBES = frozenset({"--version", "-V", "-v"})
# `python -m py_compile x.py` / `-m compileall` только компилируют в байткод и ничего не импортируют и не запускают.
_COMPILE_ONLY_MODULES = frozenset({"py_compile", "compileall"})
# .git/hooks, .git/config: исполнение кода при ближайшем git-вызове. Не путать с .gitignore/.github.
_TOUCHES_GIT = re.compile(r"(?:^|[\\/])\.git(?:[\\/]|$)", re.IGNORECASE)


def _trust_code_runners() -> bool:
    """Явное решение владельца: ``ARIA_SHELL_TRUST_CODE_RUNNERS=1`` возвращает автозапуск python/pytest/npm run."""
    return os.getenv("ARIA_SHELL_TRUST_CODE_RUNNERS", "0") == "1"

# Разделители составных команд: каждую часть проверяем отдельно.
# Одиночный «&» — разделитель в cmd.exe и фон в POSIX: тоже режем по нему.
_SHELL_SEPARATORS = re.compile(r"\|\||&&|[|;&\r\n]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Безобидные перенаправления, которые не считаем записью в файл.
_SAFE_REDIRECTS = re.compile(r"\d?>>?\s*(?:/dev/null|nul)\b|\d>&\d", re.IGNORECASE)
_REDIRECT_TARGET = re.compile(r"(?:\d?>>?|<)\s*([^\s;&|<>]+)")
_REDIRECT_TARGET_FULL = re.compile(r"(?:\d?>>?|<)\s*[^\s;&|<>]+")
_PATH_ESCAPE = re.compile(r"^(?:[/\\~]|[A-Za-z]:)|(?:^|[\\/])\.\.(?:[\\/]|$)|[$%`]")


def is_high_risk_command(command: str) -> bool:
    return any(p.search(command) for p in HIGH_RISK_PATTERNS)


def _tokens(segment: str) -> list[str]:
    """Токены сегмента. posix=False — иначе shlex съедает «\» в Windows-путях."""
    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        return []
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in raw]


def _normalize_head(tokens: list[str]) -> tuple[str, list[str]]:
    """(имя бинарника, остальные токены). ``sudo`` → пустое имя (всегда approval)."""
    tokens = list(tokens)
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0].lower() == "sudo":
        return "", tokens
    if tokens and tokens[0] in ("env", "command", "nice", "time"):
        tokens.pop(0)
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
    if not tokens:
        return "", []
    head = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if head.endswith(".exe"):
        head = head[:-4]
    return head, tokens[1:]


def _segment_allowed(segment: str) -> bool:
    head, args = _normalize_head(_tokens(segment))
    if not head or head not in SHELL_ALLOWLIST:
        return False
    flags = [a for a in args if a.startswith("-") or (head in ("del", "dir", "copy", "move") and a.startswith("/") and len(a) <= 3)]
    positional = [a for a in args if a not in flags]
    # Любой путь вне корня песочницы / с расширением переменных — подтверждение.
    if any(_PATH_ESCAPE.search(a) for a in positional):
        return False
    if any(_TOUCHES_GIT.search(a.strip("\"'")) for a in positional):
        return False
    if not _trust_code_runners():
        compile_only = head in _INTERPRETERS and len(args) >= 2 and args[0] == "-m" and args[1] in _COMPILE_ONLY_MODULES
        if head in _CODE_RUNNERS and not compile_only and not (len(args) == 1 and args[0] in _VERSION_PROBES):
            return False
        if head == "npm" and next((a for a in args if not a.startswith("-")), "") in _NPM_CODE_SUBCOMMANDS:
            return False
    if head in SUBCOMMAND_ALLOW:
        sub = next((a for a in args if not a.startswith("-")), "")
        if args and args[0].startswith("-"):  # `git -c …`, `npm --prefix …` — до подкоманды
            return False
        if sub not in SUBCOMMAND_ALLOW[head]:
            return False
    if head in _INTERPRETERS and any(a in _INLINE_CODE_FLAGS for a in args):
        return False
    if head == "find" and any(a in _FIND_DANGEROUS for a in args):
        return False
    if head in _DELETERS and any(
        re.match(r"^-[a-zA-Z]*[rR]", a) or a == "--recursive" or a.lower() == "/s" for a in args
    ):
        return False
    return True


def classify_shell_command(command: str) -> str:
    """Вернуть ``"allow"`` или ``"approval"`` для команды (§14.2 / волна 1 A13).

    Разрешено только если ВСЕ условия выполнены:
      * нет совпадения с HIGH_RISK_PATTERNS;
      * нет подстановок (``$(...)``, обратные кавычки), cmd-экранирования ``^``;
      * нет перенаправлений ``>``/``<`` (кроме в /dev/null и ``2>&1``);
      * каждый сегмент (``;``/``&&``/``||``/``|``/``&``) начинается с бинарника из
        SHELL_ALLOWLIST и проходит пер-командные правила (_segment_allowed):
        подкоманды git/pip/npm, запрет inline-кода у python/node, запрет
        ``find -exec/-delete``, запрет рекурсивного rm, запрет путей вне
        корня (абсолютные, ``..``, ``~``, ``$VAR``/``%VAR%``).
    """
    if not command or not command.strip():
        return "approval"
    if is_high_risk_command(command):
        return "approval"
    if "$(" in command or "`" in command or "^" in command:
        return "approval"
    stripped = _SAFE_REDIRECTS.sub("", command)
    # Перенаправление в относительный путь внутри песочницы — обычная работа;
    # наружу (~, абсолютный, .., $VAR) — только с подтверждением.
    for target in _REDIRECT_TARGET.findall(stripped):
        if _PATH_ESCAPE.search(target.strip("\"'")) or _TOUCHES_GIT.search(target.strip("\"'")):
            return "approval"
    stripped = _REDIRECT_TARGET_FULL.sub(" ", stripped)
    if re.search(r"[<>]", stripped):
        return "approval"
    segments = [s for s in _SHELL_SEPARATORS.split(stripped) if s.strip()]
    if not segments:
        return "approval"
    return "allow" if all(_segment_allowed(seg) for seg in segments) else "approval"


# ── R1 (19.3 PLAN-1): команда выполнима на Windows? ───────────────────────
# Проверка применимости команды на Windows: каждая часть составной команды
# (; && || | &) должна начинаться с cmd-builtin ИЛИ с бинарника из PATH
# (shutil.which). На не-Windows платформе функция всегда True. Список «unix-only»
# ниже НЕ участвует в решении True/False — только в тексте причины отказа.

# Полный набор внутренних команд cmd.exe (решение владельца 30.10: rmdir и весь
# список). `where` сюда НЕ входит — он есть как where.exe в System32 и определяется
# через shutil.which, как любая внешняя утилита.
_CMD_BUILTINS: frozenset[str] = frozenset(
    {
        "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date",
        "del", "dir", "echo", "endlocal", "erase", "exit", "for", "ftype", "goto",
        "if", "md", "mkdir", "mklink", "move", "path", "pause", "popd", "prompt",
        "pushd", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift",
        "start", "time", "title", "type", "ver", "verify", "vol",
    }
)

# Известные POSIX-утилиты: используются ТОЛЬКО для различия «unix-only» vs
# «не найдено в PATH» в сообщении отказа (см. command_rejection_reason).
_UNIX_ONLY_HINTS: frozenset[str] = frozenset(
    {"bash", "sh", "zsh", "dash", "csh", "ksh", "ls", "cat", "grep", "egrep",
     "fgrep", "sed", "awk", "head", "tail", "wc", "cut", "tr", "touch", "cp",
     "mv", "rm", "which", "iconv", "chmod", "chown", "chgrp", "sudo", "su",
     "tee", "xargs", "ln", "rmdir", "diff", "less", "more", "tar", "gzip",
     "gunzip", "unzip", "wget", "scp", "ssh", "rsync"}
)


def _is_windows_platform() -> bool:
    return os.name == "nt"


def _split_compound(command: str) -> list[str]:
    """Делит команду по ``;`` ``&&`` ``||`` ``|`` ``&`` вне кавычек."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";&|":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _first_lexeme(segment: str) -> str:
    """Первая лексема сегмента: кавычки, путь и ``.exe``/``.com`` сняты (lowercase)."""
    head, _ = _normalize_head(_tokens(segment))
    if head.endswith(".com"):
        head = head[:-4]
    return head


def is_windows_command(command: str) -> bool:
    """True, если команда выполнима на Windows (cmd-builtin или найдена в PATH).

    На не-Windows платформе всегда True: проверка имеет смысл только на Windows.
    """
    if not _is_windows_platform():
        return True
    if not command or not command.strip():
        return False
    for segment in _split_compound(command):
        head = _first_lexeme(segment)
        if not head:
            continue
        if head in _CMD_BUILTINS:
            continue
        if shutil.which(head) is not None:
            continue
        return False
    return True


def command_rejection_reason(command: str) -> str:
    """Причина, по которой ``is_windows_command`` вернул False.

    ``unix_only_command`` — известная POSIX-утилита, которой на Windows нет;
    ``command_not_found_in_path`` — инструмент может существовать (например, в venv),
    но не в текущем PATH.
    """
    for segment in _split_compound(command):
        head = _first_lexeme(segment)
        if not head:
            continue
        if head in _CMD_BUILTINS:
            continue
        if shutil.which(head) is not None:
            continue
        return "unix_only_command" if head in _UNIX_ONLY_HINTS else "command_not_found_in_path"
    return "command_not_found_in_path"


def build_dry_run_command(command: str) -> str:
    """Best-effort dry-run превью для §14.2 'approval + dry-run обязателен'.
    Для shell не существует универсального dry-run — показываем echo-превью
    и, если команда начинается с rm/mv/dd, добавляем безопасный --dry-run/-n
    флаг там, где инструмент это поддерживает."""
    stripped = command.strip()
    if stripped.startswith("rm "):
        return stripped.replace("rm ", "rm --interactive=always -v ", 1) + "  # (запрошено подтверждение перед удалением)"
    if stripped.startswith("mv "):
        return stripped + " -n  # (no-clobber, ничего не перезапишет)"
    if stripped.startswith("rsync"):
        return stripped + " --dry-run"
    return f"echo DRY-RUN: {stripped}"


class ToolValidationError(Exception):
    pass


def assert_role_allowed(role_id: str, tool_whitelist: tuple[str, ...], tool_name: str) -> None:
    if tool_name not in tool_whitelist:
        raise ToolValidationError(f"role {role_id} is not whitelisted for tool {tool_name} (§7.1 tool_whitelist)")


def assert_retry_policy(idempotency_class: IdempotencyClass, is_retry: bool) -> None:
    """§11: 'Ретрай без idempotency policy запрещён.' unsafe_write/external_side_effect
    не ретраятся автоматически без явного подтверждения на уровне вызывающего кода."""
    if is_retry and idempotency_class in (IdempotencyClass.unsafe_write, IdempotencyClass.external_side_effect):
        raise ToolValidationError(
            f"auto-retry запрещён для idempotency_class={idempotency_class.value} без approval (§11)"
        )


# ── Входная валидация аргументов тула по input_schema ────────────────────
# Раньше схема была только подсказкой для модели: handler получал что угодно и либо падал
# с невнятным ``KeyError: 'path'``, либо молча «успешно» отрабатывал пустой вызов.

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


# «approved» модели игнорируется самим shell-handler'ом (доверять ему нельзя), поэтому не ошибка.
_TOLERATED_EXTRA_KEYS = frozenset({"approved"})


def _type_ok(value, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    allowed = _JSON_TYPES.get(type_name)
    if allowed is None:
        return True
    if isinstance(value, bool) and type_name in ("integer", "number"):
        return False  # True/False — не число
    return isinstance(value, allowed)


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def validate_tool_input(schema: dict, arguments) -> list[str]:
    """Список человекочитаемых ошибок; пустой список = аргументы годны.

    Поддержано ровно то, что используется в TOOL_REGISTRY: type, required, properties.type/enum,
    array.items.type, oneOf из ``required``-вариантов. Лишние ключи не запрещаются.
    Обязательная строка не может быть пустой/из пробелов («обязательный» ≠ «присутствует»).
    """
    if not isinstance(arguments, dict):
        return [f"аргументы должны быть JSON-объектом, получено {type(arguments).__name__}"]
    errors: list[str] = []
    props: dict = schema.get("properties", {}) or {}
    for name in schema.get("required", []) or []:
        if name not in arguments or _is_blank(arguments[name]):
            errors.append(f"обязательный аргумент «{name}» не передан или пуст")
    one_of = schema.get("oneOf")
    if one_of:
        variants = [v.get("required", []) for v in one_of]
        if not any(all(not _is_blank(arguments.get(n)) for n in req) for req in variants):
            names = " или ".join("+".join(req) for req in variants)
            errors.append(f"нужен один из аргументов: {names}")
    if props:
        unknown = sorted(k for k in arguments if k not in props and k not in _TOLERATED_EXTRA_KEYS)
        if unknown:
            errors.append(
                f"неизвестные аргументы {unknown}; допустимые: {sorted(props)} "
                "(опечатка в имени аргумента иначе молча дала бы результат по умолчанию)"
            )
    for name, value in arguments.items():
        spec = props.get(name)
        if not spec or value is None:
            continue
        types = spec.get("type")
        if types:
            tlist = types if isinstance(types, list) else [types]
            if not any(_type_ok(value, t) for t in tlist):
                errors.append(f"аргумент «{name}» должен быть {'/'.join(tlist)}, получено {type(value).__name__}")
                continue
        if "enum" in spec and value not in spec["enum"]:
            errors.append(f"аргумент «{name}» должен быть одним из {spec['enum']}, получено {value!r}")
        item_type = (spec.get("items") or {}).get("type")
        if item_type and isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if not _type_ok(item, item_type):
                    errors.append(f"элемент {i} аргумента «{name}» должен быть {item_type}")
                    break
    return errors
