import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { ChatMsg } from "@/lib/api";
import { api } from "@/lib/api";

/* Tool calls are one collapsible "processes" block, not one chat bubble per
   call. Consecutive tool messages are grouped by the caller; expanding a row
   shows arguments and the (bounded) output the model actually received. */

const MAX_PREVIEW = 6000;

function preview(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text.length <= MAX_PREVIEW) return text;
  return `${text.slice(0, MAX_PREVIEW)}\n… (+${text.length - MAX_PREVIEW} chars)`;
}

export function ToolCallGroup({ messages }: { messages: ChatMsg[] }) {
  const names = messages.map((msg) => msg.tool_output?.tool_name || msg.content || "tool");
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState(() => localStorage.getItem("aria.showProcessDetails") === "1");
  useEffect(() => {
    const sync = () => setExpanded(localStorage.getItem("aria.showProcessDetails") === "1");
    window.addEventListener("aria-process-details", sync);
    return () => window.removeEventListener("aria-process-details", sync);
  }, []);
  const undo = async (messageId: string, checkpointId: string) => {
    if (!window.confirm("Restore the file from before this change?")) return;
    setBusy(messageId);
    try {
      await api.restoreCheckpoint(checkpointId);
      setNote((prev) => ({ ...prev, [messageId]: "Restored" }));
    } catch {
      setNote((prev) => ({ ...prev, [messageId]: "Undo failed" }));
    } finally {
      setBusy(null);
    }
  };
  return (
    <details open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)} className="group border border-border bg-background/40 text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-2 py-1 text-muted-foreground hover:text-foreground">
        <ChevronDown className="h-3 w-3 -rotate-90 transition-transform group-open:rotate-0" />
        <span className="shrink-0">Processes · {messages.length}</span>
        <span className="min-w-0 truncate">{names.join(" · ")}</span>
      </summary>
      <div className="space-y-2 border-t border-border/60 p-2">
        {messages.map((msg) => {
          const meta = msg.tool_output;
          const output = meta?.output;
          const outputText = output && typeof output.text === "string" ? output.text : null;
          return (
            <div key={msg.id} className="space-y-1">
              <div className="flex items-baseline gap-2">
                <span className="font-medium">{meta?.tool_name || msg.content || "tool"}</span>
                {meta?.status && <span className="text-muted-foreground">{meta.status}</span>}
                {meta?.checkpoint_id && (
                  <button
                    type="button"
                    className="ml-auto border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-foreground/5 disabled:opacity-50"
                    disabled={busy === msg.id}
                    onClick={() => void undo(msg.id, meta.checkpoint_id as string)}
                  >
                    {note[msg.id] ?? "Undo"}
                  </button>
                )}
              </div>
              {meta?.arguments && Object.keys(meta.arguments).length > 0 && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">arguments</div>
                  <pre className="max-h-40 overflow-auto whitespace-pre-wrap bg-background/60 p-1.5 text-[11px]">
                    {preview(meta.arguments)}
                  </pre>
                </div>
              )}
              {output && (
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-muted-foreground">output</div>
                  {outputText !== null ? (
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap bg-background/60 p-1.5 text-[11px]">
                      {preview(outputText)}
                    </pre>
                  ) : (
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap bg-background/60 p-1.5 text-[11px]">
                      {preview(output)}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </details>
  );
}
