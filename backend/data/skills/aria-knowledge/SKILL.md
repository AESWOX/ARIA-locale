---
name: aria-knowledge
description: Durable knowledge about the ARIA project - build pipeline, Tauri 2/MSI on Windows, FastAPI pitfalls, Loop-Engineering.
---
# ARIA Knowledge

## ARIA project
- Windows 10, git-bash, Python 3.11.15, uv package manager. Backend Python 3.11/FastAPI, Desktop React+Vite+Tauri 2.
- Backend: PyInstaller `--onefile` → `backend/dist/backend.exe` (23MB).
- v13: 135/135 tests green. v12: 37/37, 8 live providers, 0 stale, 0 legacy refs.

## Build pipeline (v13 FINAL)
- `build_release.py` → staging (фильтр RELEASE_MANIFEST.txt) → `check_secret_scan()` → `pytest` (data-dependent тесты deselected: test_vault_and_skills, test_mcp_server) → `__pycache__` cleanup → tar.
- RELEASE_MANIFEST.txt: fnmatch НЕ поддерживает `**` globstar. `desktop/src/*` для файлов в src/, `desktop/src/**/*` для вложенных.
- TSC: `vendor-env.d.ts` для `ui-kit` деклараций, `"jsx": "react-jsx"` в tsconfig.app.json.

## Tauri 2 на Windows
- Фронт (WebView2) использует Origin `http://tauri.localhost` — ОБЯЗАТЕЛЬНО в CORS allow_origins (tauri://localhost и https://tauri.localhost НЕ покрывают WebView2).
- Кастомный заголовок (X-Local-Agent-Token) триггерит preflight OPTIONS; если Origin не разрешён — 400.
- Runtime token: Rust (Uuid) → env `LOCAL_AGENT_RUNTIME_TOKEN` → фронту в `get_runtime_bootstrap` (invoke).
- `wmic process where processid=X get commandline /format:list` — узнать, кто слушает порт.
- Tauri 2 срезает target-triple суффикс у externalBin → sidecar называется `backend.exe` (не backend-x86_64-pc-windows-msvc.exe), лежит рядом с main exe.
- SPA-фоллбек должен проверять `request.scope['orig_path']` (strip_api_prefix срезает /api).
- MSI: per-machine (ALLUSERS=1) требует админ (1603/Error 1925 без elevation). Per-user: `msiexec /i x.msi /qn ALLUSERS=2 MSIINSTALLPERUSER=1`. NSIS — per-user без админа.
- `rmdir /S /Q` не удаляет папку с запущенным процессом (rc=0 молча) — сначала taskkill, потом rmdir.

## FastAPI подводные камни
- Union-аннотация возврата (FileResponse | HTMLResponse) → FastAPIError: Invalid args for response field при импорте — лечится `response_model=None`.
- Same-origin SPA: FastAPI отдаёт desktop/dist (StaticFiles + SPA-фоллбек с response_model=None!), токен в `<meta name=runtime-token>`, окно Tauri делает window.navigate на backend.

## Loop-Engineering (v1.1)
- Stage 1-7 executor, integrity-детекторы, PlanStep валидатор (freeform запрещён).
- ScanResult.clean = (critical_count == 0), NOT (len(matches)==0).
- DENY_PATTERNS: 14. Locking: TTL + max 15 retries, exponential backoff (0.5→5s).
- Executor имеет `_with_db_retry()` (3 retries). PlanHistory retention: max 20 entries.
