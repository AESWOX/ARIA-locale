# ARIA by aeswox

Локальный AI-агент для Windows: настольное приложение (Tauri + React) с бэкендом на FastAPI. История чатов, память, заметки и ключи провайдеров хранятся на вашей машине, модели подключаются по вашим API-ключам.

Агент работает в трёх режимах (Chat, Agent, Plan), выполняет опасные шаги только с вашего подтверждения и проверяет результат отдельным аудитом. Подробности и честный список того, что пока не работает: [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md). История изменений: [CHANGELOG.md](CHANGELOG.md).

## Быстрый старт из исходников

Требования: Windows 10+, Python 3.11, Node 20+, Rust/Cargo 1.79+ (только для сборки Tauri).

```powershell
# бэкенд
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env      # впишите ключи хотя бы одного провайдера
.\.venv\Scripts\python.exe run_backend.py      # API на http://127.0.0.1:8765

# интерфейс (в другом терминале)
cd desktop
npm ci
npm run dev                                    # http://127.0.0.1:1420
```

Окно Tauri вместо браузера: `npx tauri dev` в каталоге `desktop`. Установщик: `npm run tauri build` (результат в `src-tauri/target/release/bundle/`). Сборка, тесты и настройка провайдеров описаны в [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## Тесты

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
cd ..\desktop
npm run typecheck
```

В бэкенде 1433 теста.

## Проверка состояния и релиз

```powershell
python dod_verify.py            # быстрая проверка репозитория
python dod_verify.py --clean    # полная проверка с нуля (чистит .venv, npm ci)
python dod_verify.py --json     # только JSON
```

Перед выпуском релиза пройдите [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md). Релизный архив собирает `python build_release.py`: список файлов берётся из `RELEASE_MANIFEST.txt`, сканы секретов и артефактов встроены, при нарушении сборка падает.

## Структура

```
backend/          FastAPI-бэкенд (пакет aria/)
  aria/core/      цикл агента, исполнитель, аудит, подтверждения
  aria/llm/       роутер провайдеров, пулы ключей, сжатие контекста
  aria/tools/     встроенные инструменты
  aria/routers/   HTTP-маршруты
  aria/mcp/       MCP-клиент и OAuth
  tests/          pytest
desktop/          React + Vite + Tauri
  src/            интерфейс (страницы, компоненты, i18n, темы)
  src-tauri/      Rust-оболочка (sidecar backend.exe, CSP)
docs/             обзор проекта и руководство по разработке
build_release.py  сборка релизного архива
dod_verify.py     проверка состояния репозитория
```

## Конфигурация

- `backend/.env` (шаблон: `backend/.env.example`): ключи провайдеров, `POSTGRES_DSN`, лимиты расходов.
- По умолчанию используется SQLite (`POSTGRES_DSN=sqlite:///./data/local_agent.db`), это однопользовательский режим. Для нескольких пользователей укажите настоящий Postgres DSN; в серверном режиме при SQLite бэкенд пишет предупреждение в лог.
- `aria_dev_mode=True` отключает требование токена (только для разработки). По умолчанию API требует заголовок `X-Local-Agent-Token`.

## Лицензия и безопасность

Лицензия: [LICENSE](LICENSE). О найденных уязвимостях сообщайте по инструкции в [SECURITY.md](SECURITY.md). Правила участия: [CONTRIBUTING.md](CONTRIBUTING.md).
