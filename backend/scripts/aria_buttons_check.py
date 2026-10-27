#!/usr/bin/env python
"""aria_buttons_check.py — полная проверка «каждой кнопки и всей механики» ЗАПУЩЕННОГО ARIA.

Только стандартная библиотека. Лежит рядом с aria_full_check.py и e2e_smoke.py и использует их.
Ключи моделей не нужны и в отчёт не попадают.

  python aria_buttons_check.py                  # порт приложения ищется сам
  python aria_buttons_check.py --llm on --agent # + прогон aria_full_check с моделью и агентом
  python aria_buttons_check.py --no-fuzz        # без перебора мусорных запросов

Стадии (каждая PASS / WARN / FAIL / SKIP):
  ui_inventory  статический разбор desktop/src: какие вызовы API делает каждая страница (кнопки),
                есть ли такой маршрут на бэкенде, не ведёт ли кнопка на заглушку (501)
  get_sweep     все GET без параметров отвечают 2xx и валидным JSON
  flows         сценарии по каждой функции: сессии, файлы, cron, вебхуки, профили, vault, задачи,
                логи, система, аналитика, настройки, инструменты, контекст чата
  fuzz          каждый маршрут с мусорными телами: не должно быть 5xx (кроме честных 501/503)
  full_check    запуск aria_full_check.py (хуки, MCP, навыки, память, скорость, агент) и свод его итогов
  cleanup       удаление всего, что создал скрипт (префикс e2ebtn)

Что НЕ делается автоматически (остаётся на ручной чеклист приёмки): клики по окну приложения,
рендер, Approve-карточки в интерфейсе, установщик, автообновление.
Отчёты: aria_buttons_report.md и aria_buttons_report.json (папка --out, по умолчанию текущая).
Код выхода = число FAIL.

Что остаётся после прогона: папка заметок e2ebtn/ в vault (маршрута удаления заметок нет), её можно
удалить вручную или кнопкой Clean test data.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from e2e_smoke import Api, discover_token  # noqa: E402

PREFIX = "e2ebtn"
RUN = uuid.uuid4().hex[:6]
NAME = f"{PREFIX}{RUN}"          # годится для имён профилей и вебхуков: [a-z0-9]

# Маршруты, которые нельзя дёргать мусором и в обходе: гасят процесс, ходят наружу, стирают данные.
DESTRUCTIVE = (
    "shutdown", "restart", "kill", "open-external", "open_external", "/aria/update", "uninstall",
    "/system/clean", "sandbox/open", "/memory/reset", "/logs/clear", "/sessions/prune", "/sessions/empty",
    "/sessions/bulk-delete", "/ops/import", "/ops/backup", "/ops/dump", "/ops/checkpoints/prune",
    "/ops/checkpoints/restore", "/env/reveal", "/gateway/", "/storage/b2/upload", "/credentials/pool",
    "/setup/apply", "/config/raw", "/profiles/active", "/model/set", "/mcp/catalog/install",
    "/skills/hub/", "/plugins/import", "/auth/verify-pin", "/webhooks/enable",
)
FUZZ_BODIES = (None, {}, {"x": 1}, {"path": 5, "name": None, "content": [1], "id": [], "text": 7}, [1, 2], "str")
ALLOWED_5XX = (501, 503)
EXTERNAL_WARN = ("/storage/b2/",)   # внешний сервис: 5xx = предупреждение, не поломка


# ── результаты ───────────────────────────────────────────────────────────

class Warn(Exception):
    pass


class Skip(Exception):
    pass


RESULTS: list[dict] = []
DETAILS: dict[str, object] = {}
CREATED: list[tuple[str, str]] = []   # (метод, путь) для cleanup


def short(v, n=240) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def record(stage: str, name: str, status: str, note: str = "", secs: float = 0.0) -> None:
    RESULTS.append({"stage": stage, "name": name, "status": status, "note": note, "secs": round(secs, 2)})
    mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    line = f"  [{mark}] {stage:<10} {name}"
    if note and status != "PASS":
        line += f" — {short(note, 160)}"
    print(line, flush=True)


def check(stage: str, name: str, fn) -> None:
    t0 = time.time()
    try:
        note = fn()
        record(stage, name, "PASS", note if isinstance(note, str) else "", time.time() - t0)
    except Warn as exc:
        record(stage, name, "WARN", str(exc), time.time() - t0)
    except Skip as exc:
        record(stage, name, "SKIP", str(exc), time.time() - t0)
    except AssertionError as exc:
        record(stage, name, "FAIL", str(exc), time.time() - t0)
    except Exception as exc:  # noqa: BLE001 — скрипт не должен падать целиком
        tb = traceback.format_exc().strip().splitlines()[-1]
        record(stage, name, "FAIL", f"{type(exc).__name__}: {exc} ({tb})", time.time() - t0)


def expect(api: Api, method: str, path: str, body=None, ok=(200,), *, soft=False, what=""):
    """Вызов API. soft=True: 4xx → Warn (схема тела не угадана), 5xx всегда FAIL."""
    status, data = api.call(method, path, body, timeout=60)
    if status in ok:
        return data
    if status >= 500 and status not in ALLOWED_5XX:
        raise AssertionError(f"{method} {path} -> {status}: {short(data)}")
    if soft and 400 <= status < 500:
        raise Warn(f"{method} {path} -> {status}: {short(data)} {what}".strip())
    raise AssertionError(f"{method} {path} -> {status} (ждали {ok}): {short(data)}")


def find_id(data, keys=("id", "session_id", "job_id", "task_id", "name")):
    if isinstance(data, dict):
        for k in keys:
            if data.get(k):
                return str(data[k])
    return None


# ── стадия: ui_inventory ─────────────────────────────────────────────────

def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def parse_api_ts(src: str) -> dict[str, list[tuple[str, str]]]:
    """name -> [(METHOD, /path-with-{})] из объекта `export const api = {...}`."""
    start = src.find("export const api = {")
    if start < 0:
        return {}
    body = src[start:]
    out: dict[str, list[tuple[str, str]]] = {}
    entries = list(re.finditer(r"^  (\w+):", body, re.M))
    for i, m in enumerate(entries):
        end = entries[i + 1].start() if i + 1 < len(entries) else len(body)
        block = re.sub(r"\$\{[^}]*\}", "{}", body[m.end():end])
        method_m = re.search(r'method:\s*"(\w+)"', block)
        method = method_m.group(1).upper() if method_m else "GET"
        paths = []
        for u in re.findall(r"""["'`](/api/[^"'`?\s]*)""", block):
            u = u[len("/api"):]
            u = re.sub(r"(?<=[A-Za-z0-9_.\-])\{\}$", "", u)   # хвост `${query}` — это query-строка, не сегмент
            u = u.rstrip("/") or "/"
            paths.append((method, u))
        if paths:
            out[m.group(1)] = paths
    return out


def openapi_matchers(spec: dict) -> list[tuple[str, str, re.Pattern]]:
    res = []
    for path, ops in spec["paths"].items():
        rx = "^" + re.sub(r"\{[^}]+\}", "[^/]+", re.escape(path).replace("\\{", "{").replace("\\}", "}")) + "$"
        rx = re.sub(r"\\\{[^}]+\\\}", "[^/]+", rx)
        for m in ops:
            if m in ("get", "post", "put", "patch", "delete"):
                res.append((m.upper(), path, re.compile(rx)))
    return res


def match_route(matchers, method: str, path: str):
    """-> (status, openapi_path): ok | method | missing."""
    norm = re.sub(r"\{\}", "x", path)
    same_path = False
    for m, p, rx in matchers:
        if rx.match(norm) or rx.match(norm + "/"):
            if m == method:
                return "ok", p
            same_path = True
    return ("method", path) if same_path else ("missing", path)


def stub_routes(repo: Path) -> set[tuple[str, str]]:
    f = repo / "backend" / "aria" / "routers" / "stubs.py"
    if not f.exists():
        return set()
    src = _read(f)
    parts = re.split(r"(?=^@router\.(?:get|post|put|patch|delete)\()", src, flags=re.M)
    out = set()
    for part in parts:
        m = re.match(r'@router\.(\w+)\("([^"]+)"', part)
        if m and ("not_implemented" in part or "501" in part):
            out.add((m.group(1).upper(), re.sub(r"\{[^}]+\}", "{}", m.group(2))))
    return out


def resolve_imports(src_root: Path, f: Path, seen: set[Path]) -> set[Path]:
    if f in seen or not f.exists():
        return seen
    seen.add(f)
    for spec in re.findall(r"""from\s+["']((?:@/|\.{1,2}/)[^"']+)["']""", _read(f)):
        base = src_root / spec[2:] if spec.startswith("@/") else (f.parent / spec)
        for cand in (base.with_suffix(".tsx"), base.with_suffix(".ts"), base / "index.tsx"):
            if cand.exists():
                resolve_imports(src_root, cand, seen)
                break
    return seen


def st_ui_inventory(ctx):
    repo: Path = ctx["repo"]
    src_root = repo / "desktop" / "src"
    api_ts = src_root / "lib" / "api.ts"
    if not api_ts.exists():
        raise Skip(f"нет {api_ts} (--repo указывает не на корень репозитория?)")
    ctx["api"].call("GET", "/health")
    spec = ctx["openapi"]
    matchers = openapi_matchers(spec)
    methods = parse_api_ts(_read(api_ts))
    stubs = stub_routes(repo)

    status_of: dict[str, str] = {}
    for name, calls in methods.items():
        worst = "ok"
        for method, path in calls:
            st, _ = match_route(matchers, method, path)
            if st != "ok":
                worst = st
            elif (method, path) in stubs:
                worst = "stub" if worst == "ok" else worst
        status_of[name] = worst

    pages_dir = src_root / "pages"
    rows = []
    for page in sorted(pages_dir.glob("*.tsx")):
        files = resolve_imports(src_root, page, set())
        text = "\n".join(_read(f) for f in files)
        used = sorted(set(re.findall(r"\bapi\.(\w+)\(", text)) & set(methods))
        direct = sorted(set(re.sub(r"\$\{[^}]*\}", "{}", u)[len("/api"):]
                            for u in re.findall(r"""fetchJSON[^"'`]*["'`](/api/[^"'`?\s]*)""", text)))
        buttons = len(re.findall(r"onClick=", _read(page)))
        dead = [n for n in used if status_of[n] in ("missing", "method")]
        stub = [n for n in used if status_of[n] == "stub"]
        rows.append({"page": page.stem, "buttons_in_page": buttons, "api_methods": len(used), "direct_urls": len(direct),
                     "dead": dead, "stub": stub})
    DETAILS["ui_pages"] = rows
    DETAILS["ui_methods_total"] = len(methods)
    DETAILS["ui_methods_dead"] = sorted(n for n, s in status_of.items() if s in ("missing", "method"))
    DETAILS["ui_methods_stub"] = sorted(n for n, s in status_of.items() if s == "stub")

    all_dead_used = sorted({n for r in rows for n in r["dead"]})
    all_stub_used = sorted({n for r in rows for n in r["stub"]})
    record("ui_inventory", f"API-клиент: {len(methods)} вызовов, страниц {len(rows)}", "PASS")
    if all_dead_used:
        record("ui_inventory", "кнопки без маршрута на бэкенде", "FAIL", ", ".join(all_dead_used))
    else:
        record("ui_inventory", "кнопки без маршрута на бэкенде", "PASS", "нет")
    if all_stub_used:
        record("ui_inventory", "кнопки на заглушке 501 (нарушает «не показывать нерабочее»)", "WARN",
               f"{len(all_stub_used)}: " + ", ".join(all_stub_used))
    else:
        record("ui_inventory", "кнопки на заглушке 501", "PASS", "нет")
    unused = sorted(set(DETAILS["ui_methods_dead"]) - set(all_dead_used))
    if unused:
        record("ui_inventory", "мёртвые вызовы в api.ts, которые страницы не используют", "WARN", ", ".join(unused[:20]))
    nav = _read(src_root / "App.tsx") if (src_root / "App.tsx").exists() else ""
    start = nav.find("const BUILTIN_NAV_REST")
    if start >= 0:
        end = nav.find("\n];", start)
        n = 1 + len(re.findall(r'\bpath:\s*"/', nav[start:end]))
        DETAILS["nav_items"] = n
        record("ui_inventory", "пунктов навигации (цель беты: 5)", "PASS" if n == 5 else "WARN", f"сейчас {n}")
    return "__done__"


# ── стадия: get_sweep ────────────────────────────────────────────────────

def st_get_sweep(ctx):
    api: Api = ctx["api"]
    spec = ctx["openapi"]["paths"]
    routes = []
    for path, ops in sorted(spec.items()):
        get = ops.get("get")
        if get is None or "{" in path or any(s in path for s in DESTRUCTIVE):
            continue
        if any(p.get("required") for p in get.get("parameters", [])):
            continue
        routes.append(path)
    slow, bad = [], []
    for path in routes:
        t0 = time.time()
        status, data = api.call("GET", path, timeout=60)
        dt = time.time() - t0
        if dt > 1.5:
            slow.append(f"{path} {dt:.1f}с")
        if status >= 500 and status not in ALLOWED_5XX:
            if any(path.startswith(e) for e in EXTERNAL_WARN):
                continue
            bad.append(f"{path} -> {status}")
        elif status >= 400 and status not in (400, 404, 409, 422, 501, 503):
            bad.append(f"{path} -> {status}")
    record("get_sweep", f"{len(routes)} GET-маршрутов без параметров", "FAIL" if bad else "PASS", "; ".join(bad))
    if slow:
        record("get_sweep", "медленные GET (>1.5 с)", "WARN", "; ".join(slow))
    return "__done__"


# ── стадия: flows ────────────────────────────────────────────────────────

def flow_sessions(api: Api):
    made = expect(api, "POST", "/sessions", {"title": f"{NAME} session"})
    sid, tid = made.get("session_id"), made.get("task_id")
    assert sid and tid, f"нет session_id/task_id: {short(made)}"
    CREATED.append(("DELETE", f"/sessions/{sid}"))
    DETAILS["flow_task_id"] = tid
    DETAILS["flow_session_id"] = sid
    check("flows", "sessions: список содержит новую", lambda: (
        sid in json.dumps(expect(api, "GET", "/sessions?limit=50&offset=0")) or (_ for _ in ()).throw(AssertionError("нет в списке"))) and None)
    check("flows", "sessions: карточка и сообщения", lambda: (
        expect(api, "GET", f"/sessions/{sid}"), expect(api, "GET", f"/sessions/{sid}/messages"), None)[-1])
    check("flows", "sessions: переименование", lambda: expect(api, "PATCH", f"/sessions/{sid}", {"title": f"{NAME} renamed"}) and None)
    check("flows", "sessions: поиск", lambda: expect(api, "GET", f"/sessions/search?q={NAME}") and None)
    check("flows", "sessions: статистика и счётчик пустых", lambda: (
        expect(api, "GET", "/sessions/stats"), expect(api, "GET", "/sessions/empty/count"), None)[-1])
    check("flows", "sessions: экспорт markdown", lambda: expect(api, "GET", f"/sessions/{sid}/export.md", ok=(200,)) and None)
    check("flows", "sessions: состояние запуска (/run)", lambda: expect(api, "GET", f"/sessions/{sid}/run") and None)
    check("flows", "sessions: пользовательское сообщение сохраняется", lambda: _post_user_msg(api, sid))
    return sid, tid


def _post_user_msg(api: Api, sid: str):
    # mode=plan и роль assistant: проверяем запись, а не запуск модели
    status, data = api.call("POST", f"/sessions/{sid}/messages", {"content": f"{NAME} note", "role": "assistant", "mode": "plan"})
    if status >= 500:
        raise AssertionError(f"{status}: {short(data)}")
    if status >= 400:
        raise Warn(f"{status}: {short(data)}")
    got = expect(api, "GET", f"/sessions/{sid}/messages")
    assert NAME in json.dumps(got, ensure_ascii=False), "сообщение не сохранилось"


def flow_tasks(api: Api, tid: str):
    check("flows", "tasks: статус раннера", lambda: expect(api, "GET", "/tasks/runner/status", ok=(200, 503)) and None)
    check("flows", "tasks: план / дети / отчёты аудита", lambda: (
        expect(api, "GET", f"/tasks/{tid}/plan", ok=(200, 404)),
        expect(api, "GET", f"/tasks/{tid}/children"),
        expect(api, "GET", f"/tasks/{tid}/audit-reports"), None)[-1])
    check("flows", "tasks: Cancel task", lambda: expect(api, "POST", f"/tasks/{tid}/cancel", ok=(200, 409)) and None)
    check("flows", "tasks: неизвестная задача -> 404", lambda: expect(
        api, "POST", f"/tasks/{uuid.uuid4()}/cancel", ok=(404,)) and None)


def flow_files(api: Api):
    d = f"{NAME}dir"
    CREATED.append(("DELETE_FILES", d))
    check("flows", "files: создать папку", lambda: expect(api, "POST", "/files/mkdir", {"path": d}) and None)
    check("flows", "files: повторное создание -> 409", lambda: expect(api, "POST", "/files/mkdir", {"path": d}, ok=(409,)) and None)

    def upload():
        import urllib.request
        boundary = uuid.uuid4().hex
        content = "Привет, мир".encode("utf-8")
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n{d}/привет.txt\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="x.txt"\r\n'
                f'Content-Type: text/plain\r\n\r\n').encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(api.base + "/files/upload-stream", data=body, method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "X-Local-Agent-Token": api.token or ""})
        with urllib.request.urlopen(req, timeout=30) as r:
            assert r.status == 200, r.status
    check("flows", "files: загрузка файла (русское имя)", upload)
    check("flows", "files: список и чтение", lambda: (
        expect(api, "GET", f"/files?path={d}"), expect(api, "GET", f"/files/read?path={d}/%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82.txt"), None)[-1])
    check("flows", "files: выход за песочницу запрещён", lambda: expect(api, "GET", "/files?path=../x", ok=(400,)) and None)
    check("flows", "files: корень песочницы не удаляется (без recursive, проба безопасна)", lambda: _root_delete_probe(api))
    check("flows", "files: удаление папки требует recursive", lambda: expect(api, "DELETE", "/files", {"path": d}, ok=(409,)) and None)
    check("flows", "files: удаление recursive", lambda: expect(api, "DELETE", "/files", {"path": d, "recursive": True}) and None)


def _root_delete_probe(api: Api):
    """НИКОГДА не передавать recursive=true на живом приложении: сборка без фикса 0018a сотрёт песочницу.
    С фиксом корень отклоняется кодом 400 до проверки recursive; без фикса ответ 409 (каталог не пуст)."""
    status, data = api.call("DELETE", "/files", {"path": "/", "recursive": False}, timeout=30)
    if status == 400:
        return
    if status == 409:
        raise Warn("корень отклонён только требованием recursive: в установленной сборке нет фикса 0018a, нужна пересборка")
    raise AssertionError(f"DELETE / (recursive=false) -> {status}: {short(data)}")


def flow_cron(api: Api):
    made = expect(api, "POST", "/cron/jobs", {"name": f"{NAME} job", "prompt": "e2e noop", "schedule": "0 3 * * *"}, soft=True)
    jid = find_id(made, ("id", "job_id"))
    assert jid, f"нет id: {short(made)}"
    CREATED.append(("DELETE", f"/cron/jobs/{jid}"))
    check("flows", "cron: список содержит задание", lambda: (
        jid in json.dumps(expect(api, "GET", "/cron/jobs")) or (_ for _ in ()).throw(AssertionError("нет в списке"))) and None)
    check("flows", "cron: пауза и возобновление", lambda: (
        expect(api, "POST", f"/cron/jobs/{jid}/pause"), expect(api, "POST", f"/cron/jobs/{jid}/resume"), None)[-1])
    check("flows", "cron: редактирование", lambda: expect(
        api, "PUT", f"/cron/jobs/{jid}", {"updates": {"name": f"{NAME} job2"}}, soft=True) and None)
    check("flows", "cron: шаблоны и цели доставки", lambda: (
        expect(api, "GET", "/cron/blueprints"), expect(api, "GET", "/cron/delivery-targets"), None)[-1])
    check("flows", "cron: удаление", lambda: expect(api, "DELETE", f"/cron/jobs/{jid}") and None)


def flow_webhooks(api: Api):
    made = expect(api, "POST", "/webhooks", {"name": NAME, "deliver": "log", "description": "e2e"}, soft=True)
    CREATED.append(("DELETE", f"/webhooks/{NAME}"))
    assert "secret" in json.dumps(made) or made, "пустой ответ"
    check("flows", "webhooks: подписка в списке", lambda: (
        NAME in json.dumps(expect(api, "GET", "/webhooks")) or (_ for _ in ()).throw(AssertionError("нет в списке"))) and None)
    check("flows", "webhooks: приём без подписи отклоняется", lambda: _unsigned_webhook(api))
    check("flows", "webhooks: выключить/включить подписку", lambda: (
        expect(api, "PUT", f"/webhooks/{NAME}/enabled", {"enabled": False}, soft=True),
        expect(api, "PUT", f"/webhooks/{NAME}/enabled", {"enabled": True}, soft=True), None)[-1])
    check("flows", "webhooks: удаление", lambda: expect(api, "DELETE", f"/webhooks/{NAME}") and None)


def _unsigned_webhook(api: Api):
    status, data = api.call("POST", f"/webhooks/in/{NAME}", {"event": "e2e"}, token=False)
    if status < 400:
        raise AssertionError(f"вебхук без подписи принят: {status} {short(data)}")
    if status >= 500:
        raise AssertionError(f"{status}: {short(data)}")


def flow_profiles(api: Api):
    made = expect(api, "POST", "/profiles", {"name": NAME, "description": "e2e"}, soft=True)
    CREATED.append(("DELETE", f"/profiles/{NAME}"))
    check("flows", "profiles: профиль в списке", lambda: (
        NAME in json.dumps(expect(api, "GET", "/profiles")) or (_ for _ in ()).throw(AssertionError("нет в списке"))) and None)
    check("flows", "profiles: активный профиль читается", lambda: expect(api, "GET", "/profiles/active") and None)
    check("flows", "profiles: soul читается и пишется", lambda: (
        expect(api, "GET", f"/profiles/{NAME}/soul", soft=True),
        expect(api, "PUT", f"/profiles/{NAME}/soul", {"content": "e2e soul"}, soft=True), None)[-1])
    check("flows", "profiles: описание", lambda: expect(api, "PUT", f"/profiles/{NAME}/description", {"description": "e2e2"}, soft=True) and None)
    check("flows", "profiles: удаление", lambda: expect(api, "DELETE", f"/profiles/{NAME}", soft=True) and None)


def flow_vault(api: Api):
    note = f"{PREFIX}/{NAME}.md"
    check("flows", "vault: статус / структура / дерево / теги", lambda: (
        [expect(api, "GET", p, ok=(200, 503)) for p in ("/vault/status", "/vault/structure", "/vault/tree", "/vault/tags")], None)[-1])
    check("flows", "vault: запись, чтение, поиск заметки", lambda: _vault_note(api, note))


def _vault_note(api: Api, note: str):
    status, data = api.call("PUT", f"/vault/notes/{note}", {"content": f"# {NAME}\n\nпривет e2e"})
    if status in (400, 503):
        raise Warn(f"vault недоступен для записи: {status} {short(data)}")
    if status != 200:
        raise AssertionError(f"PUT -> {status}: {short(data)}")
    got = expect(api, "GET", f"/vault/notes/{note}")
    assert NAME in json.dumps(got, ensure_ascii=False), "заметка читается без содержимого"
    expect(api, "GET", f"/vault/search?q={NAME}", ok=(200,))


def flow_system(api: Api):
    check("flows", "system: self-test", lambda: _selftest(api))
    check("flows", "system: stats и timings", lambda: (expect(api, "GET", "/system/stats"), expect(api, "GET", "/system/timings"), None)[-1])
    check("flows", "system: версия не '0.0.0 (dev)'", lambda: _version(api))
    check("flows", "ops: doctor, security-audit, prompt-size", lambda: (
        expect(api, "POST", "/ops/doctor", {}, soft=True), expect(api, "POST", "/ops/security-audit", {}, soft=True),
        expect(api, "POST", "/ops/prompt-size", {}, soft=True), None)[-1])
    check("flows", "ops: контрольные точки читаются", lambda: expect(api, "GET", "/ops/checkpoints") and None)
    check("flows", "logs: чтение", lambda: expect(api, "GET", "/logs") and None)
    check("flows", "analytics: использование и модели", lambda: (
        expect(api, "GET", "/analytics/usage"), expect(api, "GET", "/analytics/models"), None)[-1])
    check("flows", "attention-items: список", lambda: expect(api, "GET", "/attention-items") and None)
    check("flows", "обновления: проверка версии", lambda: expect(api, "GET", "/aria/update/check", ok=(200, 502, 503)) and None)


def _selftest(api: Api):
    data = expect(api, "GET", "/system/self-test")
    txt = json.dumps(data, ensure_ascii=False)
    if re.search(r'"(?:ok|passed|status)"\s*:\s*(?:false|"fail)', txt, re.I):
        raise AssertionError(f"self-test сообщает о сбое: {short(data, 400)}")


def _version(api: Api):
    txt = json.dumps(expect(api, "GET", "/system/stats"))
    if "0.0.0" in txt:
        raise AssertionError("в /system/stats версия 0.0.0 (dev): в установленной сборке нет фикса 0017d, нужна пересборка")


def flow_settings(api: Api):
    for path in ("/config", "/config/schema", "/config/defaults", "/config/public"):
        check("flows", f"settings: GET {path}", lambda p=path: expect(api, "GET", p) and None)
    check("flows", "settings: переменные окружения (значения скрыты)", lambda: _env(api))
    check("flows", "models: инфо и варианты", lambda: (
        expect(api, "GET", "/model/info"), expect(api, "GET", "/model/options"), expect(api, "GET", "/providers/models", ok=(200, 503)), None)[-1])
    check("flows", "setup: список провайдеров", lambda: expect(api, "GET", "/setup/providers") and None)
    check("flows", "tools: наборы инструментов и конфиг", lambda: _toolsets(api))
    check("flows", "themes и шрифт", lambda: (expect(api, "GET", "/dashboard/themes"), expect(api, "GET", "/dashboard/font"), None)[-1])
    check("flows", "plugins: список", lambda: expect(api, "GET", "/plugins") and None)
    check("flows", "mcp: сервера и каталог", lambda: (expect(api, "GET", "/mcp/servers"), expect(api, "GET", "/mcp/catalog"), None)[-1])
    check("flows", "skills: список и содержимое первого", lambda: _skills(api))
    check("flows", "memory: список и поиск", lambda: (expect(api, "GET", "/memory"), expect(api, "GET", "/memory/items"),
                                                      expect(api, "GET", "/memory/search?q=e2e", soft=True), None)[-1])
    check("flows", "hooks: список", lambda: expect(api, "GET", "/ops/hooks") and None)


def _env(api: Api):
    data = expect(api, "GET", "/env")
    txt = json.dumps(data)
    if re.search(r"sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{20,}", txt):
        raise AssertionError("GET /env вернул значение ключа открытым текстом")


def _toolsets(api: Api):
    data = expect(api, "GET", "/tools/toolsets")
    items = data if isinstance(data, list) else data.get("toolsets", []) if isinstance(data, dict) else []
    first = next((i.get("name") for i in items if isinstance(i, dict) and i.get("name")), None)
    if first:
        expect(api, "GET", f"/tools/toolsets/{first}/config", soft=True)


def _skills(api: Api):
    data = expect(api, "GET", "/skills")
    items = data if isinstance(data, list) else data.get("skills", []) if isinstance(data, dict) else []
    if not items:
        raise Warn("навыков нет")
    nm = next((i.get("name") for i in items if isinstance(i, dict) and i.get("name")), None)
    if nm:
        expect(api, "GET", f"/skills/content?name={nm}", soft=True)


def flow_chat(api: Api):
    check("flows", "chat: статус, модели, профиль", lambda: (
        expect(api, "GET", "/chat/status"), expect(api, "GET", "/chat/models", ok=(200, 503)), expect(api, "GET", "/chat/profile"), None)[-1])
    made = {}

    def make():
        made.update(expect(api, "POST", "/chat/sessions", {"title": f"{NAME} chat"}))
        sid = find_id(made, ("session_id", "id"))
        assert sid, short(made)
        CREATED.append(("DELETE", f"/sessions/{sid}"))
        expect(api, "GET", f"/chat/sessions/{sid}/messages")
        expect(api, "GET", f"/chat/sessions/{sid}/profile", soft=True)
    check("flows", "chat: создание сессии и чтение сообщений", make)
    check("flows", "chat: без ключа отправка отвечает 503, а не 500", lambda: _chat_send_nokey(api, made))


def _chat_send_nokey(api: Api, made: dict):
    sid = find_id(made, ("session_id", "id"))
    if not sid:
        raise Skip("нет сессии")
    status, data = api.call("POST", f"/chat/sessions/{sid}/send", {"content": "ping e2e"}, timeout=120)
    if status >= 500 and status != 503:
        raise AssertionError(f"{status}: {short(data)}")


def st_flows(ctx):
    api: Api = ctx["api"]
    sid = tid = None

    def sessions():
        nonlocal sid, tid
        sid, tid = flow_sessions(api)
    check("flows", "sessions: создание", sessions)
    if tid:
        flow_tasks(api, tid)
    for fn in (flow_files, flow_cron, flow_webhooks, flow_profiles, flow_vault, flow_system, flow_settings, flow_chat):
        try:
            fn(api)
        except Warn as exc:
            record("flows", fn.__name__, "WARN", str(exc))
        except AssertionError as exc:
            record("flows", fn.__name__, "FAIL", str(exc))
        except Exception as exc:  # noqa: BLE001
            record("flows", fn.__name__, "FAIL", f"{type(exc).__name__}: {exc}")
    return "__done__"


# ── стадия: fuzz ─────────────────────────────────────────────────────────

def st_fuzz(ctx):
    api: Api = ctx["api"]
    bad: dict[tuple[str, str], str] = {}
    sent = 0
    skipped = 0
    for path, ops in sorted(ctx["openapi"]["paths"].items()):
        if any(s in path for s in DESTRUCTIVE):
            skipped += 1
            continue
        for method in ops:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            url = path
            for seg in set(re.findall(r"\{(\w+)\}", path)):
                url = re.sub(r"\{%s(?::\w+)?\}" % seg, str(uuid.uuid4()) if "id" in seg else "x", url)
            for body in FUZZ_BODIES:
                if method == "get" and body is not None:
                    continue
                status, data = api.call(method.upper(), url, body if method != "get" else None, timeout=60)
                sent += 1
                if status >= 500 and status not in ALLOWED_5XX and not any(path.startswith(e) for e in EXTERNAL_WARN):
                    bad.setdefault((method.upper(), path), f"{status} тело={short(body, 40)} -> {short(data, 60)}")
    DETAILS["fuzz_sent"] = sent
    DETAILS["fuzz_skipped_paths"] = skipped
    if bad:
        record("fuzz", f"{sent} мусорных запросов", "FAIL", "; ".join(f"{k[0]} {k[1]}: {v}" for k, v in bad.items()))
    else:
        record("fuzz", f"{sent} мусорных запросов, пропущено опасных маршрутов {skipped}", "PASS", "5xx нет")
    return "__done__"


# ── стадия: full_check (aria_full_check.py) ──────────────────────────────

def st_full_check(ctx):
    args = ctx["args"]
    script = HERE / "aria_full_check.py"
    if not script.exists():
        raise Skip("нет aria_full_check.py рядом")
    out = Path(args.out) / "full_check"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(script), "--base", ctx["base"], "--token", ctx["token"], "--llm", args.llm,
           "--out", str(out)]
    if args.agent:
        cmd.append("--agent")
    if args.skills_limit is not None:
        cmd += ["--skills-limit", str(args.skills_limit)]
    print("  … запуск aria_full_check.py (может занять несколько минут)", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=args.full_check_timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"aria_full_check не уложился в {args.full_check_timeout} с")
    lines = [ln.strip() for ln in proc.stdout.splitlines() if re.match(r"\s*\[(PASS|WARN|FAIL|SKIP)\]", ln)]
    DETAILS["full_check_lines"] = lines
    DETAILS["full_check_report"] = str(out / "aria_check_report.md")
    for ln in lines:
        m = re.match(r"\[(PASS|WARN|FAIL|SKIP)\]\s+(\S+)\s+\S+\s+(.*)", ln)
        if m:
            record("full_check", m.group(2), m.group(1), m.group(3) if m.group(1) != "PASS" else "")
    if not lines:
        raise AssertionError("aria_full_check не вернул стадий: " + short(proc.stdout + proc.stderr, 300))
    return "__done__"


# ── cleanup ──────────────────────────────────────────────────────────────

def st_cleanup(ctx):
    api: Api = ctx["api"]
    left = []
    for method, path in reversed(CREATED):
        try:
            if method == "DELETE_FILES":
                api.call("DELETE", "/files", {"path": path, "recursive": True})
            else:
                api.call(method, path)
        except Exception as exc:  # noqa: BLE001
            left.append(f"{path}: {exc}")
    # хвосты прошлых прогонов: сессии и вебхуки с префиксом
    try:
        data = api.call("GET", "/sessions?limit=200&offset=0")[1]
        items = data.get("sessions", data.get("items", [])) if isinstance(data, dict) else data
        for s in items or []:
            if isinstance(s, dict) and PREFIX in str(s.get("title", "")):
                api.call("DELETE", f"/sessions/{s.get('id') or s.get('session_id')}")
    except Exception as exc:  # noqa: BLE001
        left.append(f"сессии: {exc}")
    record("cleanup", "удалено созданное скриптом", "WARN" if left else "PASS", "; ".join(left))
    return "__done__"


# ── отчёт ────────────────────────────────────────────────────────────────

def write_reports(out: Path, base: str, args) -> None:
    out.mkdir(parents=True, exist_ok=True)
    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    (out / "aria_buttons_report.json").write_text(
        json.dumps({"base": base, "llm": args.llm, "counts": counts, "results": RESULTS, "details": DETAILS},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    md = [f"# ARIA buttons & mechanics check", "",
          f"База: `{base}`, llm={args.llm}, agent={args.agent}, прогон `{RUN}`", "",
          f"**PASS {counts['PASS']} · WARN {counts['WARN']} · FAIL {counts['FAIL']} · SKIP {counts['SKIP']}**", ""]
    for status in ("FAIL", "WARN"):
        items = [r for r in RESULTS if r["status"] == status]
        if items:
            md += [f"## {status}", ""] + [f"- **{r['stage']} / {r['name']}**: {r['note']}" for r in items] + [""]
    pages = DETAILS.get("ui_pages")
    if pages:
        md += ["## Страницы и их вызовы API", "", "| Страница | onClick в файле | Вызовов API | Без маршрута | На заглушке 501 |", "|---|---|---|---|---|"]
        for r in pages:
            md.append(f"| {r['page']} | {r['buttons_in_page']} | {r['api_methods']} | {', '.join(r['dead']) or '—'} | {', '.join(r['stub']) or '—'} |")
        md.append("")
    md += ["## Все проверки", "", "| Стадия | Проверка | Итог | Время, с | Примечание |", "|---|---|---|---|---|"]
    for r in RESULTS:
        md.append(f"| {r['stage']} | {r['name']} | {r['status']} | {r['secs']} | {short(r['note'], 160).replace('|', '/')} |")
    (out / "aria_buttons_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nОтчёты: {out / 'aria_buttons_report.md'} (и .json). Пришли .md сюда: ключей в нём нет.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Полная проверка кнопок и механики запущенного ARIA")
    ap.add_argument("--base", help="URL приложения; по умолчанию ищется сам (порт у Tauri каждый раз новый)")
    ap.add_argument("--token")
    ap.add_argument("--repo", help="корень репозитория (по умолчанию определяется по месту скрипта)")
    ap.add_argument("--llm", choices=("on", "off"), default="off")
    ap.add_argument("--agent", action="store_true", help="для aria_full_check: Approve-сценарии agent_shell и agent_mcp")
    ap.add_argument("--skills-limit", type=int, default=None)
    ap.add_argument("--no-fuzz", action="store_true")
    ap.add_argument("--no-full-check", action="store_true", help="не запускать aria_full_check.py")
    ap.add_argument("--full-check-timeout", type=int, default=1500)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    from aria_full_check import discover_base
    base = discover_base(args.base)
    if not base:
        print("FAIL: приложение не найдено. Запусти ARIA или передай --base http://127.0.0.1:<порт>")
        return 1
    try:
        token = discover_token(base, args.token)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: токен не найден: {exc}. Передай --token")
        return 1
    api = Api(base, token)
    repo = Path(args.repo) if args.repo else HERE.parents[1]
    st, spec = api.call("GET", "/openapi.json")
    if st != 200 or not isinstance(spec, dict):
        print(f"FAIL: /openapi.json -> {st}")
        return 1
    ctx = {"api": api, "base": base, "token": token, "repo": repo, "openapi": spec, "args": args}

    print("=" * 66)
    print(f"  ARIA buttons check  base={base}  llm={args.llm}  agent={args.agent}  run={RUN}")
    print("=" * 66)
    stages = [("ui_inventory", st_ui_inventory, True), ("get_sweep", st_get_sweep, True), ("flows", st_flows, True),
              ("fuzz", st_fuzz, not args.no_fuzz), ("full_check", st_full_check, not args.no_full_check)]
    for name, fn, enabled in stages:
        if not enabled:
            record(name, "стадия", "SKIP", "отключена флагом")
            continue
        t0 = time.time()
        try:
            res = fn(ctx)
            if res != "__done__":
                record(name, "стадия", "PASS", "", time.time() - t0)
        except Skip as exc:
            record(name, "стадия", "SKIP", str(exc), time.time() - t0)
        except AssertionError as exc:
            record(name, "стадия", "FAIL", str(exc), time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            record(name, "стадия", "FAIL", f"{type(exc).__name__}: {exc}", time.time() - t0)
    try:
        st_cleanup(ctx)
    except Exception as exc:  # noqa: BLE001
        record("cleanup", "стадия", "FAIL", str(exc))

    counts = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    print("-" * 66)
    print(f"  PASS {counts['PASS']}  WARN {counts['WARN']}  FAIL {counts['FAIL']}  SKIP {counts['SKIP']}")
    write_reports(Path(args.out), base, args)
    return counts["FAIL"]


if __name__ == "__main__":
    sys.exit(main())
