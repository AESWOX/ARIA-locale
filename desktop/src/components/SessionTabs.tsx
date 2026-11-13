import { useEffect, useState } from "react";
import { Download, MoreHorizontal, Pencil, Plus, Trash2, X } from "lucide-react";
import type { ChatSessionRow } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

export type TabIndicator = "approve" | "unread" | "busy" | null;

interface MenuState {
  id: string;
  x: number;
  y: number;
}

export function SessionTabs({
  sessions,
  openIds,
  activeId,
  draftActive,
  indicator,
  onOpen,
  onClose,
  onNew,
  onRename,
  onExport,
  onDelete,
}: {
  sessions: ChatSessionRow[];
  openIds: string[];
  activeId: string | null;
  draftActive: boolean;
  indicator: (id: string) => TabIndicator;
  onOpen: (id: string) => void;
  onClose: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onExport: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  const { t } = useI18n();
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [menu, setMenu] = useState<MenuState | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);

  useEffect(() => {
    if (!menu) return;
    const close = () => {
      setMenu(null);
      setConfirmDelete(null);
    };
    window.addEventListener("click", close);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [menu]);

  const commitRename = (id: string) => {
    const title = draftTitle.trim();
    setRenamingId(null);
    if (title) onRename(id, title);
  };

  const openMenu = (event: React.MouseEvent, id: string) => {
    event.preventDefault();
    event.stopPropagation();
    const x = Math.min(event.clientX, window.innerWidth - 210);
    const y = Math.min(event.clientY, window.innerHeight - 190);
    setConfirmDelete(null);
    setMenu({ id, x, y });
  };

  return (
    <div className="flex min-w-0 items-center gap-1 overflow-x-auto border-b border-border pb-1">
      {openIds.map((id) => {
        const session = sessions.find((row) => row.id === id);
        if (!session) return null;
        const status = indicator(id);
        const isActive = draftActive ? false : id === activeId;
        return (
          <div
            key={id}
            className={cn(
              "group flex shrink-0 items-center gap-1 border border-border px-2 py-1 text-xs",
              isActive ? "bg-foreground/10 text-foreground" : "text-muted-foreground hover:bg-foreground/5",
            )}
            title={session.title}
            onContextMenu={(event) => openMenu(event, id)}
            onDoubleClick={() => {
              setRenamingId(id);
              setDraftTitle(session.title);
            }}
          >
            <button type="button" className="max-w-[180px] truncate" onClick={() => onOpen(id)}>
              {renamingId === id ? (
                <input
                  autoFocus
                  className="w-[160px] border border-border bg-background px-1 text-xs"
                  value={draftTitle}
                  onChange={(event) => setDraftTitle(event.target.value)}
                  onBlur={() => commitRename(id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") commitRename(id);
                    if (event.key === "Escape") setRenamingId(null);
                  }}
                />
              ) : (
                session.title || t.common.untitled
              )}
            </button>
            {status === "approve" && <span className="h-1.5 w-1.5 rounded-full bg-amber-500" title="needs you" />}
            {status === "unread" && <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" title="new" />}
            {status === "busy" && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted-foreground" title="working" />}
            <button
              type="button"
              className="text-muted-foreground opacity-0 hover:text-foreground group-hover:opacity-100"
              aria-label="Session menu"
              title="Session menu"
              onClick={(event) => openMenu(event, id)}
            >
              <MoreHorizontal className="h-3 w-3" />
            </button>
            <button
              type="button"
              className="text-muted-foreground opacity-60 hover:text-foreground group-hover:opacity-100"
              aria-label={t.chatComposer.closeTab}
              onClick={() => onClose(id)}
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        );
      })}
      {draftActive && (
        <div className="shrink-0 border border-border bg-foreground/10 px-2 py-1 text-xs">
          {t.chatComposer.newChatTab}
        </div>
      )}
      <button
        type="button"
        className="shrink-0 border border-border p-1 text-muted-foreground hover:bg-foreground/5"
        aria-label={t.chatComposer.newChatTab}
        title={t.chatComposer.newChatTab}
        onClick={onNew}
      >
        <Plus className="h-3.5 w-3.5" />
      </button>
      <details className="relative shrink-0">
        <summary
          className="list-none border border-border px-2 py-1 text-xs text-muted-foreground hover:bg-foreground/5"
          aria-label="All sessions"
          title="All sessions"
        >
          …
        </summary>
        <div className="absolute right-0 z-40 mt-1 max-h-72 w-64 overflow-y-auto border border-border bg-background p-1 shadow-lg">
          {sessions.slice(0, 30).map((session) => (
            <button
              key={session.id}
              type="button"
              className="block w-full truncate px-2 py-1 text-left text-xs hover:bg-foreground/5"
              title={session.title}
              onClick={(event) => {
                onOpen(session.id);
                (event.currentTarget.closest("details") as HTMLDetailsElement | null)?.removeAttribute("open");
              }}
            >
              {session.title || t.common.untitled}
            </button>
          ))}
        </div>
      </details>

      {menu && (
        <div
          className="fixed z-[120] w-48 border border-border bg-background p-1 text-xs shadow-xl"
          style={{ left: menu.x, top: menu.y }}
          onClick={(event) => event.stopPropagation()}
        >
          <button
            type="button"
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-foreground/10"
            onClick={() => {
              const row = sessions.find((item) => item.id === menu.id);
              setRenamingId(menu.id);
              setDraftTitle(row?.title ?? "");
              setMenu(null);
            }}
          >
            <Pencil className="h-3.5 w-3.5" /> Rename
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-foreground/10"
            onClick={() => {
              onExport(menu.id);
              setMenu(null);
            }}
          >
            <Download className="h-3.5 w-3.5" /> {t.sessions.exportChat ?? "Export"}
          </button>
          <button
            type="button"
            className={cn(
              "flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-destructive/10",
              confirmDelete === menu.id ? "text-destructive" : "",
            )}
            onClick={() => {
              if (confirmDelete !== menu.id) {
                setConfirmDelete(menu.id);
                return;
              }
              onDelete(menu.id);
              setMenu(null);
              setConfirmDelete(null);
            }}
          >
            <Trash2 className="h-3.5 w-3.5" />
            {confirmDelete === menu.id ? "Click again to delete" : (t.sessions.deleteChat ?? "Delete")}
          </button>
          <button
            type="button"
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-foreground/10"
            onClick={() => {
              onClose(menu.id);
              setMenu(null);
            }}
          >
            <X className="h-3.5 w-3.5" /> {t.chatComposer.closeTab}
          </button>
        </div>
      )}
    </div>
  );
}
