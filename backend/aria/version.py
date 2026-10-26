"""Единственный источник версии приложения.

В упакованном backend.exe (PyInstaller) нет метаданных пакета, поэтому
запрос версии у менеджера пакетов там не работает (показывало «0.0.0 (dev)»).
Версию правит только эта константа; tests/test_version_consistency.py сверяет
её с Tauri, Cargo и npm.
"""
from __future__ import annotations

APP_VERSION = "0.2.1"
