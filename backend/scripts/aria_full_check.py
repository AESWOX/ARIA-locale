#!/usr/bin/env python3
"""aria_full_check.py — полная проверка ЗАПУЩЕННОГО ARIA: скорость, работоспособность, трасса «как и почему».

Только стандартная библиотека. Лежит рядом с e2e_smoke.py и использует его стадии и помощники.
Ключи моделей скрипту не нужны: он говорит с приложением, а приложение уже знает ключи. В отчёт
ключи и токены не попадают (маскируются).

  python aria_full_check.py --llm on --agent        # порт приложения ищется сам; при необходимости: --base http://127.0.0.1:<порт>
  python aria_full_check.py --llm off                      # без вызовов модели: инфраструктура, API, хуки (без агента), MCP
  python aria_full_check.py --llm on --skills-limit 5      # трасса только по 5 навыкам (дешевле)

Что делает (стадии; каждая PASS / WARN / FAIL / SKIP, с временем):
  infra        health/status ×20 (p50/p95/max), токен обязателен, self-test, doctor, security-audit
  surface      все GET-маршруты без параметров: код ответа и время; 5xx = FAIL, >1.5 c = WARN
  config       какие провайдеры и модели настроены (секреты маскируются)
  memory       запись, поиск, удаление: время каждой операции
  chat_speed   3 коротких запроса к модели: время ответа; затем p50/p95 по этапам из /system/timings
  tools        агент на фикстуре в песочнице: file_search / file_grep / file_read(диапазон), трасса вызовов
  skills       каждый навык: файл читается, есть описание; с --llm on агент читает навык и объясняет
               «что делает, когда нужен, какие инструменты вызвал и почему» + трасса вызовов
  hooks        временные хуки: pre_tool блокирует file_read (код 2), лог-хук видит события; хуки удаляются
  mcp          стадии e2e (фальшивый MCP, default-deny), затем тест каждого ВАШЕГО MCP-сервера (без вызова тулов)
  e2e          стадии plugin / memory / chat / agent_shell / agent_mcp из e2e_smoke (с --agent)
  logs         ошибки и предупреждения в логах за прогон
Отчёты: aria_check_report.json и aria_check_report.md (папка --out, по умолчанию текущая).
Код выхода = число FAIL.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_smoke as E  # noqa: E402  (Api, FakeMcp, Ctx, STAGES, wait_for, helpers)

LLM_T = 240
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,}|gsk_[A-Za-z0-9]{16,}|"
                       r"(?i:(?:api[_-]?key|token|secret|password)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]{8,})")
RESULTS: list[dict] = []
DETAILS: dict[str, object] = {}
SECRETS: set[str] = set()


def redact(text: str) -> str:
    text = SECRET_RE.sub("[redacted]", text)
    for s in SECRETS:
        if s and len(s) > 8:
            text = text.replace(s, "[redacted]")
    return text


def short(v, n=300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = redact(s.replace("\n", " ⏎ "))
    return s if len(s) <= n else s[:n] + "…"


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else 0.0


class Warn(Exception):
    pass


_JUNK_SKILL_RE = re.compile(r"^e2e[0-9a-f]+-hello$")


def is_e2e_junk(name: str) -> bool:
    """Навык-остаток e2e-прогона: свой шаблон плюс шаблон из e2e_smoke."""
    rx = getattr(E, "_E2E_SKILL_RE", None)
    return bool(_JUNK_SKILL_RE.match(name) or (rx and rx.match(name)))


def run_stage(name, fn, ctx, *, enabled=True, why=""):
    t0 = time.time()
    if not enabled:
        status, note = "SKIP", why
    else:
        try:
            note = fn(ctx) or ""
            status = "PASS"
        except E.Skip as exc:
            status, note = "SKIP", str(exc)
        except Warn as exc:
            status, note = "WARN", str(exc)
        except Exception as exc:  # noqa: BLE001
            if "No language model is configured" in str(exc) or "provider_unavailable" in str(exc):
                status, note = "SKIP", "модель не настроена (добавь ключ в Keys и повтори)"
            else:
                status, note = "FAIL", f"{type(exc).__name__}: {exc}"
    dur = time.time() - t0
    RESULTS.append({"stage": name, "status": status, "seconds": round(dur, 1), "note": redact(str(note))[:600]})
    print(f"  [{status:<4}] {name:<11} {dur:6.1f}s  {redact(str(note))[:150]}")


# ── общие помощники ─────────────────────────────────────────────────────

def timed(api, method, path, body=None, **kw):
    t = time.time()
    status, data = api.call(method, path, body, **kw)
    return status, data, (time.time() - t) * 1000


def sandbox_dir(api) -> Path | None:
    """Папка песочницы агента: из конфига, иначе %APPDATA%/ARIA/sandbox."""
    try:
        raw = api.ok("GET", "/config/raw")
        text = json.dumps(raw, ensure_ascii=False)
        m = re.search(r'agent_sandbox_root["\']?\s*[:=]\s*["\']([^"\']+)', text.replace("\\\\", "\\"))
        if m and Path(m.group(1)).is_dir():
            return Path(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    for base in (os.environ.get("APPDATA"), os.environ.get("XDG_DATA_HOME"), str(Path.home() / ".local" / "share")):
        if base and (Path(base) / "ARIA" / "sandbox").is_dir():
            return Path(base) / "ARIA" / "sandbox"
    return None


def make_fixture(sb: Path) -> Path:
    root = sb / "aria_check"
    shutil.rmtree(root, ignore_errors=True)
    (root / "src").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "node_modules" / "m").mkdir(parents=True)
    (root / "src" / "main.cpp").write_text("#include <x>\nint main() {\n  parse_args(argc, argv);\n  return 0;\n}\n", encoding="utf-8")
    (root / "src" / "util.h").write_text("void parse_args(int argc, char** argv);\n", encoding="utf-8")
    (root / "src" / "ru.txt").write_bytes("Ошибка разбора аргументов\n".encode("cp1251"))
    (root / "build" / "junk.cpp").write_text("parse_args junk\n", encoding="utf-8")
    (root / "node_modules" / "m" / "i.js").write_text("parse_args dep\n", encoding="utf-8")
    return root


def run_task(ctx, title, prompt, profile=None, timeout=LLM_T):
    """Агентская задача без Approve: ждёт терминального статуса. Возвращает трассу."""
    api = ctx.api
    sid = ctx.new_task_session(title)
    api.ok("PUT", f"/chat/sessions/{sid}/profile", profile or {"style": "solo", "audit": "off", "thinking": "off"})
    t0 = time.time()
    queued = api.ok("POST", f"/sessions/{sid}/messages", {"content": prompt, "mode": "agent"}, timeout=90)
    t_clar = 0.0
    if queued.get("status") == "awaiting_clarification":
        tc = time.time()
        queued = E._answer_clarifications(ctx, queued)
        t_clar = time.time() - tc
    last = {}

    def poll():
        r = api.ok("GET", f"/sessions/{sid}/run")
        last.update(r)
        return r if r["terminal"] or r["attention"] else None

    try:
        run = E.wait_for(poll, timeout, "task end", every=1.5)
    except AssertionError as exc:
        run = {**last, "status": last.get("status", "timeout"), "attention": last.get("attention", []), "terminal": False,
               "error_message": str(exc)}
    snap = api.ok("GET", f"/sessions/{sid}")
    calls = snap.get("tool_calls") or []
    msgs = [m for m in snap.get("messages", []) if m.get("role") == "assistant"]
    return {
        "sid": sid, "status": run.get("status"), "seconds": round(time.time() - t0, 1), "clarify_seconds": round(t_clar, 1),
        "error": run.get("error_code") or run.get("error_message"),
        "approvals_pending": [a.get("type") for a in run.get("attention", [])],
        "calls": [{"tool": c["tool_name"], "status": c["status"], "ms": c.get("duration_ms"),
                   "args": short(c.get("input_json"), 200), "out": short(c.get("output_json"), 260),
                   "error": c.get("error_message")} for c in calls],
        "answer": short(msgs[-1]["content"], 1500) if msgs else "",
        "messages": [short(m.get("content"), 200) for m in snap.get("messages", []) if m.get("role") == "tool"],
    }


# ── стадии ──────────────────────────────────────────────────────────────

def st_infra(ctx):
    api = ctx.api
    lat = {}
    for p in ("/health", "/status"):
        xs = []
        for _ in range(20):
            s, _, ms = timed(api, "GET", p, token=False)
            assert s == 200, f"{p} -> {s}"
            xs.append(ms)
        lat[p] = {"p50_ms": round(pct(xs, 50), 1), "p95_ms": round(pct(xs, 95), 1), "max_ms": round(max(xs), 1)}
    for tok in (False, "wrong-token"):
        s, _ = api.call("GET", "/mcp/servers", token=tok)
        assert s in (401, 403), f"без валидного токена /mcp/servers -> {s}"
    out = {"latency": lat}
    for name, path, meth in (("self_test", "/system/self-test", "GET"), ("doctor", "/ops/doctor", "POST"),
                             ("security_audit", "/ops/security-audit", "POST"), ("stats", "/system/stats", "GET")):
        s, d, ms = timed(api, meth, path, {} if meth == "POST" else None, timeout=120)
        out[name] = {"http": s, "ms": round(ms), "body": short(d, 1500)}
    DETAILS["infra"] = out
    bad = [k for k, v in out.items() if isinstance(v, dict) and v.get("http") and v["http"] >= 500]
    assert not bad, f"5xx: {bad}"
    return f"/status p50 {lat['/status']['p50_ms']} мс, p95 {lat['/status']['p95_ms']} мс"


def st_surface(ctx):
    api = ctx.api
    spec = api.ok("GET", "/openapi.json", token=False)
    rows, bad, slow, unconf, ext_down = [], [], [], [], []
    for path, ops in sorted(spec["paths"].items()):
        if "{" in path or "get" not in ops or path in ("/mcp/oauth/callback", "/system/shutdown", "/docs", "/redoc"):
            continue
        s, _, ms = timed(api, "GET", path, timeout=60)
        rows.append({"path": path, "http": s, "ms": round(ms)})
        if s == 503:
            unconf.append(path)  # сервис не настроен (например, облачное хранилище): не ошибка приложения
        elif s == 502 and path.startswith("/storage/b2"):
            ext_down.append(f"{path} {s}")  # B2 не настроен или недоступен (ключи/сеть): внешний сервис, не баг приложения
        elif s >= 500:
            bad.append(f"{path} {s}")
        elif ms > 1500:
            slow.append(f"{path} {round(ms)}мс")
    DETAILS["surface"] = {"checked": len(rows), "slowest": sorted(rows, key=lambda r: -r["ms"])[:8], "errors": bad, "slow": slow, "not_configured_503": unconf, "external_down_502": ext_down}
    assert not bad, "5xx: " + ", ".join(bad)
    if slow or ext_down:
        parts = []
        if ext_down:
            parts.append("внешний сервис не отвечает (B2: ключи или сеть?): " + ", ".join(ext_down))
        if slow:
            parts.append("медленные: " + ", ".join(slow))
        raise Warn(f"{len(rows)} маршрутов; " + "; ".join(parts))
    return f"{len(rows)} GET-маршрутов, без 5xx"


def st_config(ctx):
    api = ctx.api
    out = {}
    for name, path in (("public", "/config/public"), ("model_info", "/model/info"), ("chat_models", "/chat/models"),
                       ("toolsets", "/tools/toolsets")):
        s, d, ms = timed(api, "GET", path)
        out[name] = {"http": s, "ms": round(ms), "body": short(d, 1800)}
    DETAILS["config"] = out
    ctx.llm_ready = bool(api.call("GET", "/chat/models")[1])
    return "провайдеры/модели/наборы инструментов прочитаны (см. отчёт)"


def st_memory(ctx):
    api = ctx.api
    tag = f"chk{uuid.uuid4().hex[:6]}"
    t = {}
    s, d, t["add_ms"] = timed(api, "POST", "/memory/items", {"layer": "fact", "content": f"check marker {tag}"})
    assert s == 200, f"add -> {s} {d}"
    item = d["item"]["id"]
    s, d, t["search_ms"] = timed(api, "GET", f"/memory/search?q={tag}")
    found = tag in json.dumps(d, ensure_ascii=False)
    s2, _, t["delete_ms"] = timed(api, "DELETE", f"/memory/items/{item}")
    DETAILS["memory"] = {**{k: round(v) for k, v in t.items()}, "found": found}
    assert found, "запись не найдена поиском"
    return f"add {t['add_ms']:.0f} / search {t['search_ms']:.0f} / delete {t['delete_ms']:.0f} мс"


def st_chat_speed(ctx):
    api = ctx.api
    prompts = ["Reply with exactly the single word PONG and nothing else.",
               "Привет! Одним предложением: что такое SQLite WAL?",
               "Write a C function that reverses a string in place. Code only."]
    rows = []
    for p in prompts:
        sid = ctx.new_chat_session()
        t = time.time()
        reply = E.send_chat(ctx, sid, p)
        rows.append({"prompt": p[:50], "seconds": round(time.time() - t, 1), "reply": short(reply, 200)})
    s, d, _ = timed(api, "GET", "/system/timings")
    DETAILS["chat_speed"] = {"replies": rows, "stages": d.get("stages") if isinstance(d, dict) else d}
    mx = max(r["seconds"] for r in rows)
    if mx > 20:
        raise Warn(f"медленно: до {mx} с на простой чат (см. этапы в отчёте)")
    return "ответы: " + ", ".join(f"{r['seconds']} с" for r in rows)


def st_tools(ctx):
    sb = sandbox_dir(ctx.api)
    if not sb:
        raise E.Skip("папка песочницы не найдена (передай --sandbox)")
    root = make_fixture(sb)
    ctx.cleanups.append(lambda: shutil.rmtree(root, ignore_errors=True))
    tr = run_task(ctx, "aria check tools",
                  "Using ONLY tools, do these in order and report what each returned: "
                  "1) file_search with glob aria_check/**/*.cpp ; "
                  "2) file_grep with pattern parse_args (path aria_check) ; "
                  "3) file_read path aria_check/src/main.cpp start_line 2 end_line 3. "
                  "If a tool does not exist, say so.")
    DETAILS["tools"] = tr
    names = [c["tool"] for c in tr["calls"]]
    problems = []
    if "file_grep" not in names:
        problems.append("file_grep не вызван (нет в сборке или модель не выбрала)")
    elif any(c["tool"] == "file_grep" and ("build/" in c["out"] or "node_modules" in c["out"]) for c in tr["calls"]):
        problems.append("file_grep вернул мусор из build/node_modules")
    if any(c["status"] not in ("ok", "success") for c in tr["calls"]):
        problems.append("есть вызовы не в статусе ok")
    if tr["status"] not in ("done", "done_unaudited"):
        raise AssertionError(f"задача завершилась как {tr['status']}: {tr['error']}")
    if problems:
        raise Warn("; ".join(problems) + f" | вызовы: {names}")
    return f"{tr['seconds']} с, вызовы: {names}"


def st_skills(ctx):
    api = ctx.api
    skills = api.ok("GET", "/skills")
    rows, bad, junk = [], [], []
    for sk in skills:
        name = sk["name"]
        row = {"name": name, "enabled": sk.get("enabled"), "category": sk.get("category"),
               "description": short(sk.get("description") or "", 160)}
        s, d, ms = timed(api, "GET", f"/skills/content?name={name}")
        row["read_ms"] = round(ms)
        if s != 200:
            row["problem"] = f"content -> {s}"
        else:
            body = d.get("content", "")
            row["bytes"] = len(body.encode("utf-8"))
            if not sk.get("description"):
                row["problem"] = "нет описания (модель не поймёт, когда навык нужен)"
        if row.get("problem"):
            if is_e2e_junk(name):
                junk.append(name)  # остаток прошлых e2e-прогонов, не проблема самого навыка
            else:
                bad.append(f"{name}: {row['problem']}")
        rows.append(row)
    DETAILS["skills"] = rows
    DETAILS["skills_e2e_junk"] = junk
    probe = ctx.args.llm == "on" and ctx.llm_ready
    if probe:
        todo = [r for r in rows if r["enabled"] and not r.get("problem") and not is_e2e_junk(r["name"])]
        if ctx.args.skills_limit:
            todo = todo[: ctx.args.skills_limit]
        for r in todo:
            tr = run_task(ctx, f"aria check skill {r['name']}",
                          f"Use the skill_lookup or skill_read tool to read the skill named '{r['name']}'. Then answer in exactly "
                          "3 lines: (1) what it does, (2) when you would use it, (3) which tools you called and WHY.",
                          timeout=180)
            used = any(c["tool"] in ("skill_read", "skill_lookup") and r["name"] in (c["args"] + c["out"]) for c in tr["calls"])
            r["trace"] = {**tr, "read_the_skill": used}
            if not used:
                bad.append(f"{r['name']}: агент не прочитал навык (вызовы: {[c['tool'] for c in tr['calls']]})")
            elif tr["status"] not in ("done", "done_unaudited"):
                bad.append(f"{r['name']}: задача {tr['status']}: {tr['error']}")
    if junk:
        bad.append(f"мусор e2e-прогонов: {len(junk)} навыков (старая сборка не удаляет строку при удалении плагина; "
                   "после пересборки удалите их вручную)")
    if bad:
        raise Warn(f"{len(skills)} навыков; проблемы: " + " | ".join(bad)[:500])
    return f"{len(skills)} навыков" + (", трасса по каждому" if probe else " (без трассы: --llm on)")


def st_hooks(ctx):
    api = ctx.api
    tmp = Path(tempfile.mkdtemp(prefix="aria_hooks_"))
    log = tmp / "events.log"
    helper = tmp / "hook.py"
    helper.write_text(
        "import sys, json, os\n"
        "mode, log = sys.argv[1], sys.argv[2]\n"
        "p = sys.stdin.read()\n"
        "open(log, 'a', encoding='utf-8').write(os.environ.get('ARIA_HOOK_EVENT', '?') + ' ' + p[:300].replace('\\n', ' ') + '\\n')\n"
        "if mode == 'block':\n"
        "    sys.stderr.write('blocked by aria_full_check'); sys.exit(2)\n", encoding="utf-8")
    py = f'"{sys.executable}"'
    block_cmd = f'{py} "{helper}" block "{log}"'
    log_cmd = f'{py} "{helper}" log "{log}"'
    existing = api.ok("GET", "/ops/hooks")
    created = []
    try:
        for ev, matcher, cmd in (("pre_tool", "^file_read$", block_cmd), ("pre_tool", ".*", log_cmd), ("post_tool", ".*", log_cmd)):
            api.ok("POST", "/ops/hooks", {"event": ev, "matcher": matcher, "command": cmd, "timeout": 10, "approve": True})
            created.append((ev, cmd))
        listed = api.ok("GET", "/ops/hooks")["hooks"]
        mine = [h for h in listed if h.get("command") in (block_cmd, log_cmd)]
        assert len(mine) == 3 and all(h.get("allowed") for h in mine), f"хуки не сохранились/не одобрены: {mine}"
        info = {"hooks_before": len(existing.get("hooks", [])), "valid_events": existing.get("valid_events")}
        if ctx.args.llm == "on" and ctx.llm_ready:
            sb = sandbox_dir(api)
            if sb:
                root = make_fixture(sb)
                ctx.cleanups.append(lambda: shutil.rmtree(root, ignore_errors=True))
            tr = run_task(ctx, "aria check hooks",
                          "Using tools: 1) file_search glob aria_check/**/*.cpp ; 2) file_read path aria_check/src/main.cpp. "
                          "Report each result; if a tool call is blocked, say so and why.")
            ev = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            info.update({"agent": tr, "log_events": [short(e, 160) for e in ev[:12]]})
            blocked = any("hook_blocked" in m for m in tr["messages"]) or any("hook" in (c["error"] or "") for c in tr["calls"])
            assert any(e.startswith("pre_tool") for e in ev), "pre_tool хук не сработал (лог пуст)"
            assert any(e.startswith("post_tool") for e in ev), "post_tool хук не сработал"
            assert blocked, "pre_tool хук с кодом 2 НЕ заблокировал file_read"
            DETAILS["hooks"] = info
            return f"блокировка и события работают, событий в логе: {len(ev)}"
        DETAILS["hooks"] = info
        return "CRUD хуков работает (поведение движка проверяется при --llm on и настроенной модели)"
    finally:
        for ev, cmd in created:
            api.call("DELETE", "/ops/hooks", {"event": ev, "command": cmd})
        shutil.rmtree(tmp, ignore_errors=True)


def st_mcp(ctx):
    api = ctx.api
    E.s_mcp(ctx)  # фальшивый сервер: подключение, тулы, default-deny
    rows = []
    for s in api.ok("GET", "/mcp/servers").get("servers", []):
        if s["name"] == ctx.mcp_name:
            continue
        if not s.get("enabled"):
            rows.append({"name": s["name"], "enabled": False, "note": "выключен, не тестировался"})
            continue
        st, d, ms = timed(api, "POST", f"/mcp/servers/{s['name']}/test", {}, timeout=90)
        rows.append({"name": s["name"], "http": st, "ms": round(ms), "ok": isinstance(d, dict) and d.get("ok"),
                     "tools": len(d.get("tools", [])) if isinstance(d, dict) else None, "detail": short(d, 200)})
    DETAILS["mcp"] = {"fake_server": "ok", "user_servers": rows}
    broken = [r["name"] for r in rows if r.get("enabled", True) and not r.get("ok")]
    if broken:
        hints = []
        for r in rows:
            det = str(r.get("detail", ""))
            if r["name"] in broken and "cannot start" in det:
                cmd = det.split("cannot start '", 1)[-1].split("'", 1)[0]
                hints.append(f"{r['name']}: на машине нет '{cmd}'")
        slow = [f"{r['name']} {r['ms']}мс" for r in rows if r.get("ok") and r.get("ms", 0) > 10000]
        raise Warn("не отвечают ваши MCP-серверы: " + ", ".join(broken)
                   + (" (" + "; ".join(hints) + ")" if hints else "")
                   + ("; медленный холодный старт: " + ", ".join(slow) if slow else ""))
    return f"фальшивый сервер ok; ваших серверов: {len(rows)}"


def st_e2e(ctx):
    notes = []
    for name, needs_llm, fn in E.STAGES:
        if name not in ("plugin", "skills", "agent_shell", "agent_mcp"):
            continue
        if name in ("agent_shell", "agent_mcp") and not (ctx.args.agent and ctx.llm_ready and ctx.args.llm == "on"):
            notes.append(f"{name}: пропущен (нужны --agent и --llm on)")
            continue
        t = time.time()
        try:
            fn(ctx)
            notes.append(f"{name}: PASS {time.time() - t:.0f}с")
        except E.Skip as exc:
            notes.append(f"{name}: skip ({exc})")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{name}: FAIL {str(exc)[:200]}")
    DETAILS["e2e"] = notes
    if any("FAIL" in n for n in notes):
        raise AssertionError(" | ".join(notes))
    return " | ".join(notes)


def st_logs(ctx):
    s, d, _ = timed(ctx.api, "GET", "/logs")
    text = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False, default=str)
    text = text.replace("\\n", "\n")
    lines = text.splitlines()
    errs = [l for l in lines if re.search(r"\b(ERROR|CRITICAL|Traceback)\b|database is locked", l)]
    warns = [l for l in lines if re.search(r"\bWARNING\b", l)]
    DETAILS["logs"] = {"lines": len(lines), "errors": len(errs), "warnings": len(warns),
                       "last_errors": [short(l, 260) for l in errs[-8:]]}
    if any("database is locked" in l for l in errs):
        raise AssertionError("в логе «database is locked»")
    if errs:
        raise Warn(f"ошибок в логе: {len(errs)} (последние в отчёте)")
    return f"строк {len(lines)}, ошибок 0, предупреждений {len(warns)}"


# ── отчёт ───────────────────────────────────────────────────────────────

def write_reports(out: Path, base: str, args) -> None:
    out.mkdir(parents=True, exist_ok=True)
    data = {"base": base, "when": time.strftime("%Y-%m-%d %H:%M:%S"), "llm": args.llm, "results": RESULTS, "details": DETAILS}
    (out / "aria_check_report.json").write_text(redact(json.dumps(data, ensure_ascii=False, indent=2, default=str)), encoding="utf-8")
    md = [f"# ARIA full check {data['when']}", "", "| Стадия | Итог | Время | Заметка |", "|---|---|---|---|"]
    for r in RESULTS:
        md.append(f"| {r['stage']} | {r['status']} | {r['seconds']} с | {r['note'].replace('|', '/')[:200]} |")
    sk = DETAILS.get("skills")
    if isinstance(sk, list):
        md += ["", "## Навыки: как выполняются и почему", ""]
        for r in sk:
            md.append(f"### {r['name']} ({'on' if r['enabled'] else 'off'}) — {r.get('description')}")
            if r.get("problem"):
                md.append(f"- ПРОБЛЕМА: {r['problem']}")
            tr = r.get("trace")
            if tr:
                md.append(f"- время {tr['seconds']} с (уточнения {tr['clarify_seconds']} с), статус {tr['status']}, навык прочитан: {tr['read_the_skill']}")
                for c in tr["calls"]:
                    md.append(f"  - `{c['tool']}` {c['status']} {c['ms']} мс: {c['args']}")
                md.append(f"- ответ агента: {tr['answer']}")
    tl = DETAILS.get("tools")
    if isinstance(tl, dict):
        md += ["", "## Инструменты (трасса)", ""]
        for c in tl["calls"]:
            md.append(f"- `{c['tool']}` {c['status']} {c['ms']} мс\n  - вход: {c['args']}\n  - выход: {c['out']}")
        md.append(f"\nОтвет агента: {tl['answer']}")
    for key in ("infra", "chat_speed", "hooks", "mcp", "logs", "memory", "surface"):
        if key in DETAILS:
            md += ["", f"## {key}", "```json", json.dumps(DETAILS[key], ensure_ascii=False, indent=1, default=str)[:6000], "```"]
    (out / "aria_check_report.md").write_text(redact("\n".join(md)), encoding="utf-8")

# ---------------------------------------------------------------- автопоиск порта
# Tauri каждый раз берёт свободный порт (main.rs: find_free_port), поэтому 8765 это лишь запасной вариант.
_PORT_RX = re.compile(r"--port[ =]+(\d{2,5})")


def _run(cmd: list[str], timeout: int = 30) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return ""
    return (r.stdout or b"").decode("utf-8", errors="replace")


def _probe(base: str) -> str:
    """'' = не отвечает; 'aria' = наша главная страница с runtime-token; 'http' = отвечает, но токена на странице нет."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(base + "/", timeout=2) as r:
            html = r.read(200_000).decode("utf-8", errors="replace")
        return "aria" if 'name="runtime-token"' in html else "http"
    except urllib.error.HTTPError as exc:
        return "http" if exc.code < 500 else ""
    except Exception:  # noqa: BLE001
        return ""


def _ports_from_cmdlines() -> list[tuple[int, str]]:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match '--port' } | "
          "ForEach-Object { '' + $_.ProcessId + '|' + $_.Name + '|' + $_.CommandLine }")
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    found = []
    for line in out.splitlines():
        m = _PORT_RX.search(line)
        if m and re.search(r"backend|aria|python", line.split("|")[1] if "|" in line else line, re.I):
            found.append((int(m.group(1)), line.strip()[:90]))
    return found


def _listeners() -> list[tuple[int, int]]:
    """(порт, PID) всех слушающих TCP-сокетов на loopback (Windows: netstat -ano)."""
    out = _run(["netstat", "-ano", "-p", "TCP"])
    res = []
    for line in out.splitlines():
        m = re.search(r"TCP\s+(?:127\.0\.0\.1|\[::1\]|0\.0\.0\.0|\[::\]):(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
        if m:
            res.append((int(m.group(1)), int(m.group(2))))
    return res


def _backend_pids() -> set[int]:
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    pids = set()
    for line in out.splitlines():
        parts = [x.strip('"') for x in line.split('","')]
        if len(parts) > 1 and re.match(r"(?i)(backend|aria)[^,]*\.exe$", parts[0].strip('"')):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def discover_base(explicit: str | None, log=print) -> str | None:
    """Находит базовый URL запущенного ARIA. Порядок: --base / ARIA_BASE, командная строка процесса (--port),
    слушающие порты backend.exe, перебор всех loopback-портов, 8765."""
    from concurrent.futures import ThreadPoolExecutor
    if explicit:
        return explicit.rstrip("/")
    env = os.environ.get("ARIA_BASE")
    if env:
        return env.rstrip("/")
    tried: list[str] = []

    def pick(cands: list[int], how: str) -> str | None:
        seen, good, plain = set(), [], []
        for port in cands:
            if port in seen:
                continue
            seen.add(port)
            base = f"http://127.0.0.1:{port}"
            tried.append(str(port))
            kind = _probe(base)
            (good if kind == "aria" else plain if kind == "http" else []).append(base)
        chosen = (good or [])[:1] or []
        if chosen:
            log(f"  автопоиск ({how}): {chosen[0]}" + (f"; других кандидатов: {', '.join((good[1:] + plain))}" if len(good) + len(plain) > 1 else ""))
            return chosen[0]
        return None

    r = pick([p for p, _ in _ports_from_cmdlines()], "порт из командной строки процесса")
    if r:
        return r
    pids = _backend_pids()
    r = pick([port for port, pid in _listeners() if pid in pids], "порты backend.exe")
    if r:
        return r
    ports = sorted({port for port, _ in _listeners() if port > 1024})
    if ports:
        with ThreadPoolExecutor(max_workers=16) as ex:
            kinds = list(ex.map(lambda x: _probe(f"http://127.0.0.1:{x}"), ports))
        tried.extend(str(x) for x in ports)
        hits = [f"http://127.0.0.1:{x}" for x, k in zip(ports, kinds) if k == "aria"]
        if hits:
            log(f"  автопоиск (перебор loopback-портов): {hits[0]}")
            return hits[0]
    if _probe("http://127.0.0.1:8765") == "aria":
        return "http://127.0.0.1:8765"
    log("  автопоиск не нашёл ARIA. Проверены порты: " + (", ".join(tried) or "нет слушающих") +
        ". Запущено ли приложение? Либо укажи --base http://127.0.0.1:<порт>")
    return None



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=None, help="URL приложения; по умолчанию ищется сам (порт у Tauri каждый раз новый)")
    ap.add_argument("--token", default=None)
    ap.add_argument("--llm", choices=("on", "off"), default="on")
    ap.add_argument("--agent", action="store_true", help="также Approve-сценарии agent_shell и agent_mcp (e2e)")
    ap.add_argument("--skills-limit", type=int, default=0, help="трасса не более чем по N навыкам (0 = по всем)")
    ap.add_argument("--sandbox", default=None, help="папка песочницы агента, если не находится сама")
    ap.add_argument("--only", default="", help="стадии через запятую")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()
    args.skills = "real"
    args.base = discover_base(args.base)
    if not args.base:
        return 1
    try:
        token = E.discover_token(args.base, args.token)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: приложение не отвечает на {args.base}: {exc}")
        return 1
    SECRETS.add(token)
    api = E.Api(args.base, token)
    ctx = E.Ctx(api, args)
    ctx.llm_ready = False
    global sandbox_dir
    if args.sandbox:
        _orig = sandbox_dir
        sandbox_dir = lambda a: Path(args.sandbox)  # noqa: E731
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    print("=" * 66)
    print(f"  ARIA full check  base={args.base}  llm={args.llm}  agent={args.agent}")
    print("=" * 66)
    llm = args.llm == "on"
    plan = [("infra", st_infra, True, ""), ("surface", st_surface, True, ""), ("config", st_config, True, ""),
            ("memory", st_memory, True, ""), ("chat_speed", st_chat_speed, llm, "--llm off"),
            ("tools", st_tools, llm, "--llm off"), ("skills", st_skills, True, ""), ("hooks", st_hooks, True, ""),
            ("mcp", st_mcp, True, ""), ("e2e", st_e2e, True, ""), ("logs", st_logs, True, "")]
    for name, fn, enabled, why in plan:
        if only and name not in only:
            continue
        run_stage(name, fn, ctx, enabled=enabled, why=why)
        if name == "infra" and RESULTS[-1]["status"] == "FAIL":
            print("  инфраструктура не работает, дальше нет смысла")
            break
    for fn in reversed(ctx.cleanups):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
    write_reports(Path(args.out), args.base, args)
    c = {k: sum(1 for r in RESULTS if r["status"] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    print("-" * 66)
    print(f"  PASS {c['PASS']}  WARN {c['WARN']}  FAIL {c['FAIL']}  SKIP {c['SKIP']}")
    print(f"  отчёты: {Path(args.out).resolve() / 'aria_check_report.md'} (и .json). Пришли .md сюда: ключей в нём нет.")
    return c["FAIL"]


if __name__ == "__main__":
    sys.exit(main())
