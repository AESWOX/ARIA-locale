# Разработка: запуск, тесты, сборка, настройка

Практическое руководство для тех, кто запускает ARIA из исходников или собирает установщик. Обзор проекта и его состояния: [PROJECT_STATUS.md](PROJECT_STATUS.md).

Команды ниже даны для Windows PowerShell, выполняются из корня репозитория, если не сказано иное.

## 1. Требования

Windows 10+, Python 3.11, Node 20+, Rust/Cargo 1.79+ (только для сборки Tauri).

## 2. Запуск из исходников

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

Окно Tauri вместо браузера: `npx tauri dev` в каталоге `desktop`. В режиме разработки данные лежат в `backend/data`.

## 3. Тесты

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
cd ..\desktop
npm run typecheck
```

Тесты бэкенда идут в случайном порядке (`pytest-randomly`). Если порядок отключить (`-p no:randomly`), падают 6 WebSocket-тестов из `tests/test_security_hardening_2026_10.py`: это известная зависимость от порядка запуска, внутри своего файла они проходят.

## 4. Сборка установщика одной командой

```powershell
.\backend\.venv\Scripts\python.exe backend\scripts\rebuild_and_smoke.py
```

Скрипт проходит 13 фаз и останавливается на первой упавшей: проверка окружения, тесты, `typecheck`, репетиция на исходниках, сборка фронтенда, PyInstaller, репетиция на собранном `backend.exe`, Tauri, резервная копия данных, установка, запуск, живой смоук, закрытие. Итог: строка `RESULT: ALL GREEN` или список упавших фаз. Полные логи каждой фазы: `reports\rebuild_<время>\`.

Полезные флаги:

| Флаг | Что делает |
|---|---|
| `--dry-run` | печатает план, ничего не выполняет |
| `--no-install` | только тесты, репетиции и сборка, без установки и живого смоука |
| `--live-only` | только живой смоук уже установленного приложения |
| `--kind nsis` | установщик NSIS (не требует прав администратора); MSI ставится на всю машину и требует запуска PowerShell от администратора |
| `--no-ctx` | не проверять сжатие контекста (экономит квоту модели) |
| `--skip-tests`, `--skip-rehearsal`, `--skip-build`, `--no-relaunch` | пропуск отдельных этапов |

Живой смоук работает с вашим реальным профилем: создаёт и удаляет тестовые сессии, заметку памяти, MCP-сервер `e2e-smoke` и временный плагин, тратит несколько запросов к модели. Резервная копия профиля делается до установки.

Репетиция без ключей и без вашего профиля (фальшивая модель, изолированный каталог данных):

```powershell
.\backend\.venv\Scripts\python.exe backend\scripts\e2e_rehearsal.py
.\backend\.venv\Scripts\python.exe backend\scripts\e2e_rehearsal.py --exe backend\dist\backend.exe   # на собранном sidecar
```

### Сборка по шагам

```powershell
cd backend
.\.venv\Scripts\python.exe -m PyInstaller backend.spec --noconfirm --clean     # в backend.spec должно быть console=False
New-Item -ItemType Directory -Force ..\desktop\src-tauri\bin | Out-Null
Copy-Item dist\backend.exe ..\desktop\src-tauri\bin\backend-x86_64-pc-windows-msvc.exe -Force
cd ..\desktop
npm run tauri build      # установщики: src-tauri\target\release\bundle\{msi,nsis}
```

Папка `src-tauri\bin` не отслеживается git: создавайте её сами. Перед копированием убедитесь, что `backend.exe` свежий (дата сборки).

## 5. Данные и настройка

Установленное приложение хранит всё (база, заметки, навыки, `.env`, логи) в `%APPDATA%\ARIA`. Переопределить каталог можно переменной `ARIA_DATA_DIR`. Шаблон настроек: `backend/.env.example`. Ключи лучше вводить на странице Env в приложении, а не вставлять в чаты и командную строку.

### Провайдеры и модели

- Несколько ключей на провайдера задаются через запятую: `GEMINI_API_KEYS`, `GROQ_API_KEYS`. Пул ротирует ключи, исчерпавший квоту ключ откладывается на время, которое назвал провайдер.
- Цепочки моделей Gemini и Groq подбираются сами по списку, доступному вашему ключу. Явные переменные главнее автоподбора: `GEMINI_FLASH_MODELS`, `GEMINI_PRO_MODELS`, `COMPRESSION_MODELS` (через запятую, порядок равен порядку попыток), `GROQ_MODELS`. Отключить автоподбор: `GEMINI_MODELS_AUTO=0`, `GROQ_MODELS_AUTO=0`.
- Проверить и обновить цепочки вручную:

```powershell
.\backend\.venv\Scripts\python.exe backend\scripts\gemini_autoconfig.py --dry-run     # показать, ничего не сохраняя
.\backend\.venv\Scripts\python.exe backend\scripts\gemini_autoconfig.py --write-env   # сохранить и записать цепочки в .env
cd backend
.\.venv\Scripts\python.exe -m aria.llm.groq_discovery --probe                         # то же для Groq
```

Проба тратит по одному крошечному запросу на модель. Она проверяет, что модель отвечает, но не что умеет вызывать инструменты. После правки `.env` приложение нужно закрыть полностью и открыть заново.

- Квота бесплатного Gemini считается на проект Google Cloud и модель, а не на ключ: ключи из одного проекта делят квоту, и ёмкость увеличивает число моделей в цепочке. Реальные лимиты показывает консоль aistudio.google.com/rate-limit.
- Лимиты расходов: `BUDGET_DAILY_USD`, `BUDGET_WEEKLY_USD` (0 выключает). Порог сжатия контекста: `COMPRESSION_TOKEN_THRESHOLD`. Период обновления каталога моделей: `ARIA_CATALOG_REFRESH_SEC` (по умолчанию 1800).

## 6. Диагностика

```powershell
Get-Content "$env:APPDATA\ARIA\logs\backend.log" -Encoding UTF8 | Select-String -NotMatch "httpx|GET /" | Select-Object -Last 30
Get-CimInstance Win32_Process | Where-Object { $_.Name -match "backend|local-agent" } | Select-Object ProcessId, ParentProcessId, Name
taskkill /F /T /PID <id>      # останавливать дерево процессов целиком
```

| Симптом | Причина и решение |
|---|---|
| `HTTP 404 ... model not found` | в настройках задано имя модели, которого нет у провайдера; возьмите имя из списка API провайдера. Такое звено откладывается на 10 минут, запрос уходит дальше по цепочке |
| `HTTP 429` | исчерпана квота модели; цепочка переходит к следующей, текст итоговой ошибки перечисляет все попытки (`tried: ...`) |
| `HTTP 402 Insufficient Balance` | на счёте провайдера нет средств; пополните или уберите ключ |
| `HTTP 503 high demand` | перегрузка модели, временно; повтор и запасные модели отработают сами |
| `Errno 10048` на порту 8765 | остался дочерний процесс бэкенда от прошлого запуска; остановите его деревом, см. выше |
| Тихая установка MSI падает | MSI требует прав администратора; используйте PowerShell от администратора или NSIS |
| Кракозябры в выводе консоли | консоль читает UTF-8 как cp1251; читайте с `-Encoding UTF8`, файлы при этом не повреждены |
| Поиск в вебе возвращает блокировку | DuckDuckGo чаще блокирует IP дата-центров; тул вернёт понятную ошибку, а не пустой список |

## 7. Прочие скрипты

| Скрипт | Для чего |
|---|---|
| `backend/scripts/e2e_smoke.py` | сквозной смоук по HTTP (те же стадии, что в `rebuild_and_smoke.py`) |
| `backend/scripts/live_smoke.py` | проверка живой сети: поиск, `web_fetch`, Groq, Gemini Vision |
| `backend/scripts/export_chats.py` | выгрузка переписок, вызовов инструментов и задач из базы (`--list`, `--last N`); ключи вырезаются по шаблонам, но перед отправкой файл надо прочитать |
| `backend/scripts/clarify_eval.py` | оценка качества уточняющих вопросов |
| `dod_verify.py` | быстрая проверка состояния репозитория (`--clean` с нуля, `--json` только JSON) |
| `build_release.py` | сборка релизного архива: список файлов из `RELEASE_MANIFEST.txt`, сканы секретов и артефактов встроены |

Перед выпуском релиза пройдите [RELEASE_CHECKLIST.md](../RELEASE_CHECKLIST.md).
