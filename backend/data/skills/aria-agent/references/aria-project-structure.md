# Aria Agent — Project Overview

Aria is a complete AI agent platform built with a UI-first philosophy. Created as a sellable product.

## Project Location

`WORK/ARIA/` — full project directory (23KB package, 13 files)

## Distribution Package

`WORK/ARIA/dist/aria-agent-1.0.0-windows.zip` — ready-to-distribute 23KB

## File Structure

```
ARIA/
├── bin/aria.js              ← CLI launcher (Node.js)
├── config/config.yaml       ← Full config (provider, tools, ui, plugins, gateways)
├── core/
│   ├── agent/aria_agent.py  ← Agent core (interactive CLI session, Aria UI formatting)
│   ├── gateway/telegram_gateway.py  ← Full Telegram bot with voice/photo/document support
│   └── web/api.py           ← FastAPI backend (chat, config, logs, skills endpoints)
├── web/ui/index.html        ← Beautiful dark-theme web interface with sidebar + chat
├── installer/
│   ├── windows/INSTALL.bat  ← Windows installer (checks deps, creates dirs, configures PATH)
│   ├── windows/INSTALL.nsi  ← NSIS installer script for .exe packaging
│   └── aria-install.sh      ← macOS/Linux installer
├── scripts/build.py         ← Cross-platform build/packaging script (windows/linux/macos)
├── package.json             ← Node.js package definition
├── README.md                ← Full product documentation
└── LICENSE.md               ← MIT Core + Commercial Premium
```

## Key Differentiators

| Feature | Aria |
|---------|------|
| Response format | UI components (status bar + table + actions), never raw text |
| Web interface | Built-in (port 8190), dark theme, sidebar, chat panel |
| Telegram gateway | Full bot with voice, photo, document handling |
| CLI | `aria` command in terminal |
| The 3-Line Rule | No response exceeds 3 lines of text without a visual component |
| Action panels | Every response ends with `→ [Action]` buttons |

## How to Use

```bash
# Start interactive session
aria

# Launch web UI
aria --web

# Run setup wizard
aria setup

# Show status
aria status
```

## License Model

- **MIT**: Core agent — free for everyone
- **Commercial**: Premium skills/plugins — sold separately

## Distribution Channels

1. NSIS .exe installer (Windows)
2. bash installer (macOS/Linux)
3. GitHub: aria-labs/aria-agent
4. Website: aria-agent.ai
