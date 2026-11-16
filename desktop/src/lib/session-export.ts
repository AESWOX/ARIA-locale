import { api, getRuntimeToken } from "@/lib/api";

/** The server names the export (title + `.md`); use its Content-Disposition, not a fake `.json`. */
export function exportFilename(disposition: string | null, id: string): string {
  if (disposition) {
    const extended = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    if (extended?.[1]) {
      try {
        return decodeURIComponent(extended[1]);
      } catch {
        /* malformed header: fall through to the plain name */
      }
    }
    const plain = /filename="([^"]+)"/i.exec(disposition);
    if (plain?.[1]) return plain[1];
  }
  return `session-${id}.md`;
}

/** Download the same Markdown export from Sessions, from anywhere in the UI. */
export async function downloadSessionExport(id: string): Promise<string> {
  const headers: Record<string, string> = {
    "X-ARIA-Session-Token":
      (window as unknown as { __ARIA_SESSION_TOKEN__?: string }).__ARIA_SESSION_TOKEN__ ?? "",
  };
  const runtimeToken = getRuntimeToken();
  if (runtimeToken) headers["X-Local-Agent-Token"] = runtimeToken;
  const res = await fetch(api.exportSessionUrl(id), { credentials: "include", headers });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const name = exportFilename(res.headers.get("content-disposition"), id);
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
  return name;
}
