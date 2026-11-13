import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import "./lib/externalLinks";
import App from "./App";
import { WindowChrome } from "./components/WindowChrome";
import { SystemActionsProvider } from "./contexts/SystemActions";
import { I18nProvider } from "./i18n";
import { exposePluginSDK } from "./plugins";
import { ThemeProvider, initSurface } from "./themes";

initSurface();

// The WebView's native menu (Back/Reload/Save as/Print) is not part of ARIA and
// "Save as" writes the HTML page into Downloads. ARIA has its own menus.
document.addEventListener("contextmenu", (event) => event.preventDefault());
document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey)) return;
  const key = event.key.toLowerCase();
  if (key === "s" || key === "p") event.preventDefault();
});

/** The splash (static markup in index.html) stays at least this long after navigation start. */
const SPLASH_MIN_MS = 1500;
/** If React never gets to dismiss the splash (render crash), remove it anyway. */
const SPLASH_FAILSAFE_MS = 20000;

function dismissSplash(): void {
  const el = document.getElementById("boot-splash");
  if (!el) return;
  const wait = Math.max(0, SPLASH_MIN_MS - performance.now());
  window.setTimeout(() => {
    el.style.opacity = "0";
    window.setTimeout(() => el.remove(), 450);
  }, wait);
}

/**
 * Tauri first loads this page from its own origin while the backend sidecar starts, then
 * navigates the window to the backend, which serves the same page with the runtime token.
 * On the shell origin there is no backend to talk to: keep the splash and do not mount.
 */
const onShellOrigin =
  window.location.hostname === "tauri.localhost" || window.location.protocol === "tauri:";

if (!onShellOrigin) {
  exposePluginSDK();

  createRoot(document.getElementById("root")!).render(
    <BrowserRouter>
      <I18nProvider>
        <ThemeProvider>
          <SystemActionsProvider>
            <WindowChrome />
            <App />
          </SystemActionsProvider>
        </ThemeProvider>
      </I18nProvider>
    </BrowserRouter>,
  );

  requestAnimationFrame(() => requestAnimationFrame(dismissSplash));
  window.setTimeout(dismissSplash, SPLASH_FAILSAFE_MS);
}
