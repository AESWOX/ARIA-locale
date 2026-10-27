; Tauri NSIS hooks. After the app files are copied, one more phase downloads and
; unpacks uv (needed by MCP servers that start through uvx) into $INSTDIR\uv.
; A failed download (offline, firewall) does NOT abort the installation.

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "ARIA: installing uv (runtime for MCP servers)..."
  StrCpy $R9 "$INSTDIR\install_uv.ps1"
  IfFileExists "$R9" +2 0
    StrCpy $R9 "$INSTDIR\resources\install_uv.ps1"
  IfFileExists "$R9" +3 0
    DetailPrint "ARIA: install_uv.ps1 not found, uv skipped"
    Goto aria_uv_done
  nsExec::ExecToLog 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$R9" -Dest "$INSTDIR\uv"'
  Pop $0
  StrCmp $0 "0" +2 0
    DetailPrint "ARIA: uv was not installed (code $0). MCP server fetch will not work until uv is installed."
  aria_uv_done:
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  RMDir /r "$INSTDIR\uv"
  Delete "$INSTDIR\install_uv.ps1"
!macroend
