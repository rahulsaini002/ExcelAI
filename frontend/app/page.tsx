"use client";

import { useEffect, useRef, useState } from "react";
import {
  processSpreadsheet,
  downloadResult,
  resultToFile,
  canDownload,
  undoOp,
  checkHealth,
  inspectFiles,
  CancelledError,
  type ProcessResult,
  type HealthResult,
  type InspectResult,
  type InspectTable,
} from "@/lib/api";

const EXAMPLES = [
  "Email ke basis pe duplicate rows hata do",
  "रेवेन्यू के हिसाब से घटते क्रम में सॉर्ट करो",
  "Merge both files into one",
  "Add a Total column = Qty × Price",
];

// A sensible cap on instruction length (a notice shows as you approach it).
const MAX_INSTRUCTION = 4000;

type OkResult = Extract<ProcessResult, { status: "ok" }>;

type Message =
  | { role: "user"; text: string; files?: string[] }
  | { role: "assistant"; kind: "question"; text: string }
  | { role: "assistant"; kind: "result"; result: OkResult }
  | { role: "assistant"; kind: "error"; text: string }
  | { role: "assistant"; kind: "info"; text: string };

// One session = one continuous branch of work.
type Session = {
  id: string; // UI id (keying / switching)
  backendId: string; // server data id — regenerated when the user detaches the data
  title: string; // legacy field (kept for old saved sessions); display uses sessionTitle()
  customTitle?: string; // a name the user typed (always wins)
  aiTitle?: string; // the AI's short title for the task (first step that has one)
  messages: Message[];
  files: File[];
  started: boolean; // server holds working data for this session
  dataLabel?: string; // label of carried-over working data, e.g. "merged.xlsx · 22 rows"
  pendingQuestion: string | null; // last clarifying question (for the answer-box cosmetics)
};

function uid(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return "s-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
  }
}

function makeSession(): Session {
  return {
    id: uid(),
    backendId: uid(),
    title: "New session",
    messages: [],
    files: [],
    started: false,
    pendingQuestion: null,
  };
}

const truncate = (t: string, n = 38) => (t.length > n ? t.slice(0, n) + "…" : t);

// Recent conversation as plain text, so the backend can interpret follow-up
// instructions that depend on earlier ones.
function historyText(messages: Message[], max = 6): string {
  return messages
    .slice(-max)
    .map((m) => {
      if (m.role === "user") return `User: ${m.text}`;
      if (m.kind === "question") return `Sumio asked: ${m.text}`;
      if (m.kind === "info") return `Sumio: ${m.text}`;
      if (m.kind === "error") return `Sumio (error): ${m.text}`;
      if (m.kind === "result") return `Sumio did: ${m.result.notes.join("; ")}`;
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// The most recent successful result in a session (for auto-continue recovery).
function lastOkResult(messages: Message[]): OkResult | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "assistant" && m.kind === "result") return m.result;
  }
  return null;
}

// --- session naming ------------------------------------------------------- //
const ACTION_LABELS: Record<string, string> = {
  sort: "sort", filter: "filter", limit: "top N", remove_duplicates: "dedupe",
  fill_missing: "fill blanks", drop_missing: "drop blanks", drop_invalid: "drop invalid",
  trim: "trim", flag_missing: "flag blanks", add_formula_column: "formula",
  lookup: "lookup", aggregate: "aggregate", find_replace: "replace",
  rename_columns: "rename", drop_columns: "drop cols", select_columns: "select cols",
  format_cells: "format", merge: "merge", combine_sheets: "combine",
};

// A compact label of the distinct operations done across the whole session.
function opsLabel(messages: Message[]): string {
  const seen: string[] = [];
  for (const m of messages) {
    if (m.role === "assistant" && m.kind === "result") {
      for (const a of m.result.actions ?? []) {
        const label = ACTION_LABELS[a] ?? a;
        if (!seen.includes(label)) seen.push(label);
      }
    }
  }
  return seen.slice(0, 4).join(", ");
}

// The original uploaded file name for this session (from the first user message).
function firstFile(messages: Message[]): string | null {
  const u = messages.find((m) => m.role === "user" && m.files && m.files.length);
  return u && u.role === "user" && u.files ? u.files[0] : null;
}

// The display name: manual rename > AI title > operation summary > first instruction.
function sessionTitle(s: Session): string {
  if (s.customTitle) return s.customTitle;
  if (s.aiTitle) return s.aiTitle;
  const ops = opsLabel(s.messages);
  if (ops) return ops;
  const u = s.messages.find((m) => m.role === "user");
  if (u && u.role === "user") return truncate(u.text);
  if (s.title && s.title !== "New session") return s.title; // legacy saved sessions
  return "New session";
}

// The sidebar subtitle: file name + step count.
function sessionSubtitle(s: Session): string {
  const steps = s.messages.filter((m) => m.role === "assistant" && m.kind === "result").length;
  const stepText = steps === 0 ? "no steps yet" : `${steps} step${steps === 1 ? "" : "s"}`;
  const file = firstFile(s.messages);
  return file ? `${file} · ${stepText}` : stepText;
}

// Per-session display titles, with a counter appended to identical AUTO titles so
// duplicates (e.g. several "name the columns") are still distinguishable.
function sidebarTitles(sessions: Session[]): Map<string, string> {
  const counts = new Map<string, number>();
  for (const s of sessions) {
    const t = sessionTitle(s);
    counts.set(t, (counts.get(t) ?? 0) + 1);
  }
  const running = new Map<string, number>();
  const out = new Map<string, string>();
  for (const s of sessions) {
    const t = sessionTitle(s);
    if (!s.customTitle && (counts.get(t) ?? 0) > 1) {
      const n = (running.get(t) ?? 0) + 1;
      running.set(t, n);
      out.set(s.id, `${t} (${n})`);
    } else {
      out.set(s.id, t);
    }
  }
  return out;
}


export default function Home() {
  const [sessions, setSessions] = useState<Session[]>(() => [makeSession()]);
  const [activeId, setActiveId] = useState<string>(sessions[0].id);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [health, setHealth] = useState<HealthResult | null>(null);
  const [dragging, setDragging] = useState(false);
  const [editing, setEditing] = useState<number | null>(null); // index of message being edited inline
  const [editText, setEditText] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [dark, setDark] = useState(false);
  const [preview, setPreview] = useState<InspectResult | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const loaded = useRef(false); // becomes true once saved sessions are restored
  const abortRef = useRef<AbortController | null>(null); // cancels an in-flight request

  function toggleTheme() {
    const next = !document.documentElement.classList.contains("dark");
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("sumio_theme", next ? "dark" : "light");
    } catch {}
    setDark(next);
  }

  function showToast(msg: string) {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 1600);
  }

  function copyText(t: string) {
    navigator.clipboard?.writeText(t).then(
      () => showToast("Copied to clipboard"),
      () => showToast("Couldn't copy"),
    );
  }

  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const active = sessions.find((s) => s.id === activeId) ?? sessions[0];

  // Always-current mirror of `sessions`, so event handlers (send/rerun) read the
  // LATEST attached files instead of a stale render-closure copy. Without this, a
  // file attached just before clicking Send could be missed, and an empty request
  // would hit the backend (cryptic "upload a spreadsheet to start").
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;

  useEffect(() => {
    checkHealth().then(setHealth);
    setDark(document.documentElement.classList.contains("dark"));
  }, []);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [active.messages, loading]);

  // Auto-grow the instruction box to fit a long instruction (up to a max, then it
  // scrolls), so the user can read back everything they typed. Runs whenever the
  // text changes — including programmatic clears (after send) and example chips.
  const INPUT_MAX_H = 220; // px — ~10 lines, then it scrolls
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto"; // reset so shrinking works too
    el.style.height = Math.min(el.scrollHeight, INPUT_MAX_H) + "px";
    el.style.overflowY = el.scrollHeight > INPUT_MAX_H ? "auto" : "hidden";
  }, [input]);

  // Preview the structure of attached files (Feature 1.1) before any operation.
  const filesKey = active.files.map((f) => f.name + f.size).join("|");
  useEffect(() => {
    if (active.files.length === 0) {
      setPreview(null);
      setPreviewLoading(false);
      return;
    }
    let cancelled = false;
    setPreviewLoading(true);
    inspectFiles(active.files).then((res) => {
      if (!cancelled) {
        setPreview(res);
        setPreviewLoading(false);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filesKey]);

  // Restore saved sessions once (transcripts/summaries survive a refresh).
  useEffect(() => {
    try {
      const raw = localStorage.getItem("sumio_sessions");
      if (raw) {
        const data = JSON.parse(raw);
        if (data && Array.isArray(data.sessions) && data.sessions.length) {
          const restored: Session[] = data.sessions.map((s: Session) => ({ ...s, files: [] }));
          setSessions(restored);
          const aid = restored.some((s) => s.id === data.activeId)
            ? data.activeId
            : restored[restored.length - 1].id;
          setActiveId(aid);
        }
      }
    } catch {}
    loaded.current = true;
  }, []);

  // Persist sessions whenever they change. File objects can't be stored (files: []),
  // and the heavy result bytes (file_base64) are ALWAYS stripped — serializing a big
  // base64 string into localStorage is slow and can crash the tab. The download_id
  // stays, so downloads still work (while the server keeps the file).
  useEffect(() => {
    if (!loaded.current) return;
    const slim = sessions.map((s) => ({
      ...s,
      files: [] as File[],
      messages: s.messages.map((m) =>
        m.role === "assistant" && m.kind === "result"
          ? { ...m, result: { ...m.result, file_base64: null } }
          : m,
      ),
    }));
    try {
      localStorage.setItem("sumio_sessions", JSON.stringify({ sessions: slim, activeId }));
    } catch {}
  }, [sessions, activeId]);

  const answering = active.pendingQuestion !== null;

  function patchSession(id: string, updater: Partial<Session> | ((s: Session) => Partial<Session>)) {
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, ...(typeof updater === "function" ? updater(s) : updater) } : s)),
    );
  }

  function addFiles(incoming: FileList | File[] | null) {
    if (!incoming) return;
    const list = Array.from(incoming);
    patchSession(activeId, (s) => {
      const byKey = new Map(s.files.map((f) => [f.name + f.size, f]));
      for (const f of list) byKey.set(f.name + f.size, f);
      return { files: Array.from(byKey.values()) };
    });
  }
  function removeFile(target: File) {
    patchSession(activeId, (s) => ({
      files: s.files.filter((f) => !(f.name === target.name && f.size === target.size)),
    }));
  }

  function newSession() {
    // Reuse the current one if it's still empty (avoid blank duplicates).
    if (active.messages.length === 0 && active.files.length === 0) {
      setInput("");
      return;
    }
    const s = makeSession();
    setSessions((prev) => [...prev, s]);
    setActiveId(s.id);
    setInput("");
  }

  function switchSession(id: string) {
    setActiveId(id);
    setInput("");
  }

  function deleteSession(id: string) {
    setSessions((prev) => {
      const next = prev.filter((s) => s.id !== id);
      if (next.length === 0) {
        const fresh = makeSession();
        setActiveId(fresh.id);
        return [fresh];
      }
      if (id === activeId) setActiveId(next[next.length - 1].id);
      return next;
    });
  }

  function commitRename(id: string) {
    const t = renameText.trim();
    if (t) patchSession(id, { customTitle: truncate(t, 60) });
    setRenamingId(null);
  }

  // Stop an in-flight request (the backend keeps no partial state).
  function cancelRequest() {
    abortRef.current?.abort();
  }

  // Apply a /process response to the session. Returns false ONLY when the backend
  // lost the session (so the caller can try to auto-continue from the last result).
  function applyResult(sid: string, res: ProcessResult): boolean {
    if (res.status === "clarify") {
      patchSession(sid, (cur) => ({
        pendingQuestion: res.clarification,
        messages: [...cur.messages, { role: "assistant", kind: "question", text: res.clarification }],
      }));
      return true;
    }
    if (res.status === "ok") {
      patchSession(sid, (cur) => ({
        messages: [...cur.messages, { role: "assistant", kind: "result", result: res }],
        started: true,
        files: [],
        dataLabel: `${res.filename} · ${res.row_count} rows`,
        // Name the session from the AI's first suggested title (Option 3); the
        // operation-summary fallback (Option 1) is derived live in sessionTitle().
        aiTitle: cur.aiTitle || res.ai_title || undefined,
      }));
      return true;
    }
    if (res.status === "message") {
      patchSession(sid, (cur) => ({
        messages: [...cur.messages, { role: "assistant", kind: "info", text: res.message }],
      }));
      return true;
    }
    if (/upload a spreadsheet to start/i.test(res.error)) return false; // recoverable
    patchSession(sid, (cur) => ({
      messages: [...cur.messages, { role: "assistant", kind: "error", text: res.error }],
    }));
    return true;
  }

  // The backend forgot this session (e.g. it restarted). Transparently re-attach the
  // last result we produced and retry the SAME instruction, so the user never has to
  // re-upload. Only if the result is truly unavailable do we ask for the file.
  async function autoContinue(sid: string, instruction: string, history: string) {
    const s = sessionsRef.current.find((x) => x.id === sid);
    const last = s ? lastOkResult(s.messages) : null;
    if (s && last && canDownload(last)) {
      try {
        const file = await resultToFile(last);
        const freshId = uid();
        patchSession(sid, { files: [], started: false, dataLabel: undefined, backendId: freshId });
        setLoading(true);
        const controller = new AbortController();
        abortRef.current = controller;
        let res2: ProcessResult;
        try {
          res2 = await processSpreadsheet([file], instruction, freshId, -1, history, controller.signal);
        } catch (err) {
          abortRef.current = null;
          setLoading(false);
          if (err instanceof CancelledError) {
            showToast("Request cancelled");
            return;
          }
          patchSession(sid, (cur) => ({ messages: [...cur.messages, { role: "assistant", kind: "error", text: "Something went wrong. Please try again." }] }));
          return;
        }
        abortRef.current = null;
        setLoading(false);
        if (applyResult(sid, res2)) return; // recovered (or a normal error) — done
      } catch {
        setLoading(false); // couldn't rebuild the file (server lost it too) — fall through
      }
    }
    patchSession(sid, (cur) => ({
      messages: [...cur.messages, {
        role: "assistant", kind: "error",
        text: "Your file isn’t loaded on the server anymore (it may have restarted) and I couldn’t recover it automatically. Please re-attach it below and send again.",
      }],
      started: false,
      dataLabel: undefined,
    }));
  }

  async function send(e?: React.FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    const sid = activeId;
    const s = sessionsRef.current.find((x) => x.id === sid);
    if (!s || loading) return;
    if (!text) {
      showToast("Please type what you'd like done.");
      return;
    }
    if (s.files.length === 0 && !s.started) {
      showToast("Please upload a spreadsheet first.");
      return;
    }

    const sentFiles = s.files;
    // The recent conversation (incl. any clarifying question) is sent as `history`,
    // so the Brain can interpret a genuine ANSWER in context — while each message is
    // still its OWN instruction. (Previously every message after a clarification was
    // forced to extend the FIRST instruction, so a new task kept getting the old
    // instruction's clarification, e.g. asking about "Revenue" you never mentioned.)
    const history = historyText(s.messages);

    patchSession(sid, (cur) => ({
      messages: [
        ...cur.messages,
        { role: "user", text, files: cur.files.length ? cur.files.map((f) => f.name) : undefined },
      ],
      pendingQuestion: null,
    }));
    setInput("");

    setLoading(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let res: ProcessResult;
    try {
      res = await processSpreadsheet(sentFiles, text, s.backendId, -1, history, controller.signal);
    } catch (err) {
      setLoading(false);
      abortRef.current = null;
      if (err instanceof CancelledError) {
        showToast("Request cancelled");
        return;
      }
      patchSession(sid, (cur) => ({
        messages: [...cur.messages, { role: "assistant", kind: "error", text: "Something went wrong. Please try again." }],
      }));
      return;
    }
    abortRef.current = null;
    setLoading(false);

    // The backend forgot the session (e.g. it restarted) -> auto-continue from the
    // last result instead of making the user re-upload.
    if (!applyResult(sid, res)) {
      await autoContinue(sid, text, history);
    }
  }

  // Re-run an existing instruction IN PLACE (Retry, or Edit with new text):
  // drop that step's result and everything after it, roll the server back to the
  // state just before this step, and run it again.
  async function rerun(userIndex: number, newText?: string) {
    if (loading) return;
    const sid = activeId;
    const s = sessionsRef.current.find((x) => x.id === sid);
    if (!s) return;
    const msg = s.messages[userIndex];
    if (!msg || msg.role !== "user") return;
    const text = (newText ?? msg.text).trim();
    if (!text) return;

    // How many completed results came before this step → where to rewind to.
    const priorResults = s.messages
      .slice(0, userIndex)
      .filter((m) => m.role === "assistant" && m.kind === "result").length;

    const kept = s.messages.slice(0, userIndex);
    patchSession(sid, {
      messages: [...kept, { role: "user", text, files: msg.files }],
      pendingQuestion: null,
    });
    setEditing(null);

    setLoading(true);
    const controller = new AbortController();
    abortRef.current = controller;
    let res: ProcessResult;
    try {
      res = await processSpreadsheet([], text, s.backendId, priorResults, historyText(kept), controller.signal);
    } catch (err) {
      setLoading(false);
      abortRef.current = null;
      if (err instanceof CancelledError) {
        showToast("Request cancelled");
        return;
      }
      patchSession(sid, (cur) => ({
        messages: [...cur.messages, { role: "assistant", kind: "error", text: "Something went wrong. Please try again." }],
      }));
      return;
    }
    abortRef.current = null;
    setLoading(false);

    // Re-running an earlier step needs the session's history; if the server lost it,
    // auto-continue isn't well-defined, so ask for the file with a clear reason.
    if (!applyResult(sid, res)) {
      patchSession(sid, (cur) => ({
        messages: [...cur.messages, { role: "assistant", kind: "error", text: "Your file isn’t loaded on the server anymore (it may have restarted). Please re-attach it and try again." }],
        started: false,
        dataLabel: undefined,
      }));
    }
  }

  function startEdit(index: number, text: string) {
    setEditing(index);
    setEditText(text);
  }

  // Stop using the carried-over working data: the next prompt won't build on it.
  // (Uses a fresh backend id so the server won't chain on the old data.)
  function detachData() {
    patchSession(activeId, {
      started: false,
      dataLabel: undefined,
      backendId: uid(),
      pendingQuestion: null,
    });
  }

  // Use a generated result as a fresh working file — one click, no download/re-upload.
  // It becomes the only attached file, on a clean server context. For large files the
  // bytes are streamed from the server (not held inline), so this is async.
  async function continueWithResult(r: OkResult) {
    try {
      const file = await resultToFile(r);
      patchSession(activeId, {
        files: [file],
        started: false,
        dataLabel: undefined,
        backendId: uid(),
        pendingQuestion: null,
      });
      showToast(`Loaded ${r.filename} as the working file`);
    } catch {
      showToast("That result is no longer available — re-run the step.");
    }
  }

  // Undo the last completed step: pop the server state and drop that turn from the
  // transcript so the user can recover from a mistake (US-011).
  async function undoLast() {
    const s = sessionsRef.current.find((x) => x.id === activeId);
    if (!s || loading) return;
    const res = await undoOp(s.backendId);
    if (!res.ok) {
      showToast(res.error || "Nothing to undo.");
      return;
    }
    patchSession(activeId, (cur) => {
      const msgs = [...cur.messages];
      let ri = msgs.length - 1;
      while (ri >= 0) {
        const m = msgs[ri];
        if (m.role === "assistant" && m.kind === "result") break;
        ri--;
      }
      if (ri < 0) return {};
      let ui = ri;
      while (ui >= 0 && msgs[ui].role !== "user") ui--; // the instruction that produced it
      return {
        messages: msgs.slice(0, ui >= 0 ? ui : ri),
        dataLabel: res.primary ? `${res.primary} · ${(res.row_count ?? 0).toLocaleString()} rows` : cur.dataLabel,
      };
    });
    showToast(
      (res.steps_remaining ?? 0) > 0 ? "Undid the last step" : "Back to the original file",
    );
  }

  // Download a result, with a clear toast if the file has expired on the server
  // (common for results from an older session in history).
  async function download(r: OkResult) {
    try {
      await downloadResult(r);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "Couldn't download — use Retry on this step.");
    }
  }

  // Global shortcuts: Ctrl/Cmd+K = new session, Esc = stop a running request.
  // No dep array → re-subscribes each render so it always calls fresh handlers.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        newSession();
      } else if (e.key === "Escape" && abortRef.current) {
        cancelRequest();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  return (
    <div className="flex h-screen flex-col bg-gradient-to-b from-indigo-50 via-background to-background dark:from-indigo-950/20 lg:flex-row">
      {/* ---------------- LEFT: sessions (one branch each) ---------------- */}
      <aside className="flex max-h-[38vh] shrink-0 flex-col gap-3 overflow-y-auto border-b border-zinc-200 bg-white/70 p-4 backdrop-blur lg:max-h-none lg:w-64 lg:border-b-0 lg:border-r dark:border-zinc-800 dark:bg-zinc-900/60">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-md shadow-indigo-500/30">
            <SheetIcon className="h-5 w-5" />
          </div>
          <h1 className="flex-1 bg-gradient-to-r from-indigo-600 to-violet-600 bg-clip-text text-lg font-bold leading-none text-transparent dark:from-indigo-400 dark:to-violet-400">
            Sumio
          </h1>
          <button
            onClick={toggleTheme}
            title={dark ? "Switch to light" : "Switch to dark"}
            className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-500 transition hover:bg-zinc-100 hover:text-zinc-800 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
          >
            {dark ? <SunIcon className="h-4 w-4" /> : <MoonIcon className="h-4 w-4" />}
          </button>
          <BackendStatus health={health} />
        </div>
        <button
          onClick={newSession}
          className="flex w-full items-center justify-center gap-2 rounded-xl border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-semibold text-indigo-700 transition hover:bg-indigo-100 dark:border-indigo-900 dark:bg-indigo-950/40 dark:text-indigo-300"
        >
          <PlusIcon className="h-4 w-4" /> New session
        </button>
        <SectionTitle>Sessions</SectionTitle>
        <ul className="space-y-1.5">
          {(() => {
            const titleById = sidebarTitles(sessions);
            return [...sessions].reverse().map((s) => {
              const title = titleById.get(s.id) ?? sessionTitle(s);
              const isRenaming = renamingId === s.id;
              return (
                <li key={s.id} className="group/sess relative">
                  {isRenaming ? (
                    <input
                      autoFocus
                      value={renameText}
                      onChange={(e) => setRenameText(e.target.value)}
                      onBlur={() => commitRename(s.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitRename(s.id);
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                      className="w-full rounded-lg border border-indigo-400 bg-white px-2.5 py-2 text-xs outline-none dark:bg-zinc-800"
                    />
                  ) : (
                    <button
                      onClick={() => switchSession(s.id)}
                      onDoubleClick={() => {
                        setRenamingId(s.id);
                        setRenameText(title);
                      }}
                      title="Click to open · double-click to rename"
                      className={`w-full rounded-lg border px-2.5 py-2 pr-7 text-left transition hover:shadow-sm ${
                        s.id === activeId
                          ? "border-indigo-300 bg-indigo-50 shadow-sm dark:border-indigo-800 dark:bg-indigo-950/40"
                          : "border-zinc-200 bg-zinc-50 hover:border-indigo-300 dark:border-zinc-800 dark:bg-zinc-800/40"
                      }`}
                    >
                      <div className="truncate text-xs font-medium">{title}</div>
                      <div className="mt-0.5 truncate text-[10px] text-zinc-400">{sessionSubtitle(s)}</div>
                    </button>
                  )}
                  {!isRenaming && (
                    <button
                      onClick={() => deleteSession(s.id)}
                      title="Delete session"
                      aria-label={`Delete session ${title}`}
                      className="absolute right-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded text-zinc-400 opacity-0 transition hover:bg-red-100 hover:text-red-600 group-hover/sess:opacity-100 dark:hover:bg-red-950/50"
                    >
                      <TrashIcon className="h-3 w-3" />
                    </button>
                  )}
                </li>
              );
            });
          })()}
        </ul>
      </aside>

      {/* ---------------- CENTER: conversation + upload/input ---------------- */}
      <section
        className="relative flex min-h-0 flex-1 flex-col"
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(e) => {
          if (e.currentTarget === e.target) setDragging(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          addFiles(e.dataTransfer.files);
        }}
      >
        {dragging && (
          <div className="pointer-events-none absolute inset-0 z-10 m-3 flex items-center justify-center rounded-2xl border-2 border-dashed border-indigo-400 bg-indigo-50/80 backdrop-blur-sm dark:bg-indigo-950/50">
            <div className="flex flex-col items-center gap-2 text-indigo-600 dark:text-indigo-300">
              <UploadIcon className="h-8 w-8" />
              <span className="text-sm font-semibold">Drop files to add them</span>
            </div>
          </div>
        )}
        <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-8">
          <div className="mx-auto max-w-2xl space-y-3">
            {active.files.length > 0 && (
              <div className="msg-in">
                <PreviewPanel loading={previewLoading} preview={preview} />
              </div>
            )}
            {active.files.length > 0 && active.messages.length === 0 && (
              <div className="msg-in">
                <SuggestedActions
                  preview={preview}
                  onPick={(t) => {
                    setInput(t);
                    textareaRef.current?.focus();
                  }}
                />
              </div>
            )}
            {active.messages.length === 0 && active.files.length === 0 && <EmptyState />}
            {active.messages.map((m, i) => (
              <div key={i} className="msg-in">
                {editing === i && m.role === "user" ? (
                  <EditBox
                    value={editText}
                    onChange={setEditText}
                    onSave={() => rerun(i, editText)}
                    onCancel={() => setEditing(null)}
                  />
                ) : (
                  <MessageBubble
                    message={m}
                    onCopy={copyText}
                    onRetry={() => rerun(i)}
                    onEdit={() => startEdit(i, m.role === "user" ? m.text : "")}
                    onContinue={continueWithResult}
                    onDownload={download}
                  />
                )}
              </div>
            ))}
            {loading && <TypingDots />}
            <div ref={bottomRef} />
          </div>
        </div>

        {/* Bottom: upload + instruction, centered */}
        <div className="shrink-0 border-t border-zinc-200 bg-white/80 px-4 py-3 backdrop-blur sm:px-8 dark:border-zinc-800 dark:bg-zinc-900/70">
          <form onSubmit={send} className="mx-auto max-w-2xl space-y-2">
            {!answering && active.messages.length === 0 && (
              <div className="flex flex-wrap justify-center gap-2">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => setInput(ex)}
                    className="rounded-full border border-zinc-200 bg-zinc-50 px-3 py-1 text-xs text-zinc-600 transition hover:border-indigo-400 hover:text-indigo-600 dark:border-zinc-700 dark:bg-zinc-800/50 dark:text-zinc-400"
                  >
                    {ex}
                  </button>
                ))}
              </div>
            )}

            {/* Attached files shown as ChatGPT-style cards, removable any time */}
            {active.files.length > 0 && (
              <div className="flex flex-wrap gap-2">
                {active.files.map((f) => (
                  <div
                    key={f.name + f.size}
                    className="pop-in flex items-center gap-2 rounded-xl border border-zinc-200 bg-white py-1.5 pl-2 pr-1.5 shadow-sm dark:border-zinc-700 dark:bg-zinc-800"
                  >
                    <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-indigo-100 text-indigo-600 dark:bg-indigo-950/60 dark:text-indigo-300">
                      <FileIcon className="h-4 w-4" />
                    </span>
                    <span className="flex min-w-0 flex-col leading-tight">
                      <span className="max-w-[180px] truncate text-xs font-medium" title={f.name}>
                        {f.name}
                      </span>
                      <span className="text-[10px] text-zinc-400">{formatSize(f.size)}</span>
                    </span>
                    <button
                      type="button"
                      onClick={() => removeFile(f)}
                      aria-label={`Remove ${f.name}`}
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-700 dark:hover:bg-zinc-700 dark:hover:text-zinc-200"
                    >
                      <XIcon className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Carried-over working data shown explicitly + removable, so it's never
                used silently. Remove it to make the next prompt not use the data. */}
            {active.started && active.files.length === 0 && (
              <div className="flex flex-wrap items-center justify-center gap-2">
                <span className="pop-in inline-flex items-center gap-2 rounded-full border border-indigo-200 bg-indigo-50 px-3 py-1 text-[11px] text-indigo-700 dark:border-indigo-900 dark:bg-indigo-950/40 dark:text-indigo-300">
                  <SheetIcon className="h-3 w-3" />
                  <span className="font-medium">Your next step builds on this result</span>
                  {active.dataLabel && <span className="text-indigo-400">· {active.dataLabel}</span>}
                  <button
                    type="button"
                    onClick={detachData}
                    title="Start fresh instead — don't build on this result"
                    aria-label="Stop using this data"
                    className="ml-0.5 rounded-full p-0.5 hover:bg-indigo-100 dark:hover:bg-indigo-900"
                  >
                    <XIcon className="h-3 w-3" />
                  </button>
                </span>
                {active.messages.some((m) => m.role === "assistant" && m.kind === "result") && (
                  <button
                    type="button"
                    onClick={undoLast}
                    disabled={loading}
                    title="Undo the last step"
                    className="pop-in inline-flex items-center gap-1 rounded-full border border-zinc-200 bg-white px-3 py-1 text-[11px] font-medium text-zinc-600 transition hover:border-indigo-300 hover:text-indigo-600 disabled:opacity-40 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300"
                  >
                    <UndoIcon className="h-3 w-3" /> Undo last
                  </button>
                )}
              </div>
            )}

            <div className="flex items-end gap-2">
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                title="Attach spreadsheets"
                className="flex h-[44px] w-[44px] shrink-0 items-center justify-center rounded-xl border border-zinc-300 text-zinc-500 transition hover:border-indigo-400 hover:text-indigo-600 dark:border-zinc-700"
              >
                <UploadIcon className="h-5 w-5" />
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".csv,.xlsx,.xlsm,.xls"
                onChange={(e) => {
                  addFiles(e.target.files);
                  e.target.value = "";
                }}
                className="hidden"
              />
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                rows={1}
                maxLength={MAX_INSTRUCTION}
                placeholder={
                  answering
                    ? "Type your answer…"
                    : active.started && active.files.length === 0
                      ? "Type your next step — it builds on the result above (e.g. “now sort by date”)"
                      : active.files.length > 0
                        ? "Tell me what to do — e.g. “sort by Revenue descending” or “Revenue ke hisaab se sort karo”"
                        : "Upload a file (left button), then tell me what to do…"
                }
                className="min-h-[44px] flex-1 resize-none rounded-xl border border-zinc-300 bg-transparent px-3.5 py-2.5 text-sm outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 dark:border-zinc-700"
              />
              {loading ? (
                <button
                  type="button"
                  onClick={cancelRequest}
                  title="Stop this request"
                  aria-label="Stop this request"
                  className="flex h-[44px] shrink-0 items-center justify-center gap-2 rounded-xl border border-zinc-300 bg-white px-5 text-sm font-semibold text-zinc-700 shadow-sm transition hover:bg-zinc-50 active:scale-95 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200"
                >
                  <StopIcon className="h-3.5 w-3.5" /> Stop
                </button>
              ) : (
                <button
                  type="submit"
                  className="flex h-[44px] shrink-0 items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-indigo-600 to-violet-600 px-5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/25 transition hover:from-indigo-500 hover:to-violet-500 active:scale-95"
                >
                  {answering ? "Send" : "Run"}
                </button>
              )}
            </div>
            {input.length > MAX_INSTRUCTION * 0.8 && (
              <p className={`text-center text-[11px] ${input.length >= MAX_INSTRUCTION ? "text-amber-600 dark:text-amber-400" : "text-zinc-400"}`}>
                {input.length}/{MAX_INSTRUCTION} characters
                {input.length >= MAX_INSTRUCTION ? " — maximum length reached" : ""}
              </p>
            )}
            <p className="text-center text-[11px] text-zinc-400">
              <kbd className="rounded border border-zinc-300 px-1 dark:border-zinc-600">Enter</kbd> to{" "}
              {answering ? "answer" : "run"} ·{" "}
              <kbd className="rounded border border-zinc-300 px-1 dark:border-zinc-600">Shift+Enter</kbd> new line ·{" "}
              <kbd className="rounded border border-zinc-300 px-1 dark:border-zinc-600">Ctrl+K</kbd> new ·{" "}
              <kbd className="rounded border border-zinc-300 px-1 dark:border-zinc-600">Esc</kbd> stop
            </p>
          </form>
        </div>
      </section>

      {/* ---------------- RIGHT: Session Summary ---------------- */}
      <aside className="flex max-h-[38vh] shrink-0 flex-col gap-3 overflow-y-auto border-t border-zinc-200 bg-white/70 p-4 backdrop-blur lg:max-h-none lg:w-72 lg:border-t-0 lg:border-l dark:border-zinc-800 dark:bg-zinc-900/60">
        <SectionTitle>Session Summary</SectionTitle>
        <SummaryPanel messages={active.messages} onContinue={continueWithResult} onDownload={download} />
      </aside>

      {/* transient feedback toast */}
      {toast && (
        <div className="pop-in fixed bottom-5 left-1/2 z-50 -translate-x-1/2 rounded-full bg-zinc-900 px-4 py-2 text-xs font-medium text-white shadow-lg shadow-black/20 dark:bg-zinc-100 dark:text-zinc-900">
          {toast}
        </div>
      )}
    </div>
  );
}

function TypingDots() {
  return (
    <div className="flex items-center gap-1.5 px-1 text-zinc-400">
      {[0, 0.2, 0.4].map((d) => (
        <span
          key={d}
          className="h-2 w-2 rounded-full bg-current"
          style={{ animation: "blink 1.2s infinite", animationDelay: `${d}s` }}
        />
      ))}
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">{children}</h2>;
}

function fmtCell(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

// Feature 1.1 — the confirmation/preview panel: sheets, columns + types, row
// count and a sample table, shown after upload before any operation.
function PreviewPanel({ loading, preview }: { loading: boolean; preview: InspectResult | null }) {
  if (loading) {
    return (
      <div className="flex items-center gap-2 rounded-2xl border border-zinc-200 bg-white p-4 text-sm text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900">
        <Spinner className="h-4 w-4" /> Reading your file…
      </div>
    );
  }
  if (!preview) return null;
  if (preview.status === "error") {
    return (
      <div className="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
        {preview.error}
      </div>
    );
  }
  return (
    <div className="space-y-3 rounded-2xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <p className="text-sm font-semibold">Here&apos;s what I found</p>
      {preview.tables.map((t) => (
        <div key={t.name} className="space-y-2 rounded-xl border border-zinc-100 p-3 dark:border-zinc-800">
          <div className="flex flex-wrap items-center gap-1.5">
            <SheetIcon className="h-4 w-4 shrink-0 text-indigo-500" />
            <span className="text-sm font-medium">{t.name}</span>
            <span className="text-xs text-zinc-400">
              · {t.row_count.toLocaleString()} rows · {t.columns.length} columns
            </span>
          </div>
          {t.note && <p className="text-xs font-medium text-amber-600 dark:text-amber-400">{t.note}</p>}
          <div className="flex flex-wrap gap-1.5">
            {t.columns.map((c) => (
              <span
                key={c.name}
                className="inline-flex items-center gap-1 rounded-md border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 text-[11px] dark:border-zinc-700 dark:bg-zinc-800/50"
              >
                <span className="font-medium">{c.name}</span>
                <span className="rounded bg-indigo-100 px-1 text-[9px] uppercase text-indigo-600 dark:bg-indigo-950/60 dark:text-indigo-300">
                  {c.type}
                </span>
              </span>
            ))}
          </div>
          {t.sample_rows.length > 0 && (
            <div className="overflow-x-auto rounded-lg border border-zinc-100 dark:border-zinc-800">
              <table className="w-full text-left text-[11px]">
                <thead className="bg-zinc-50 text-zinc-500 dark:bg-zinc-800/50">
                  <tr>
                    {t.columns.map((c) => (
                      <th key={c.name} className="whitespace-nowrap px-2 py-1 font-medium">
                        {c.name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {t.sample_rows.map((row, ri) => (
                    <tr key={ri} className="border-t border-zinc-100 dark:border-zinc-800">
                      {t.columns.map((c) => (
                        <td key={c.name} className="whitespace-nowrap px-2 py-1 text-zinc-600 dark:text-zinc-300">
                          {fmtCell(row[c.name])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// Common actions derived from the uploaded file's columns, so a user who doesn't
// know what to ask gets one-click starting points (US-004). Clicking fills the
// prompt (so they can review/tweak before running).
function suggestionsFor(preview: InspectResult | null): string[] {
  if (!preview || preview.status !== "ok" || preview.tables.length === 0) return [];
  const cols = preview.tables[0].columns;
  const num = cols.find((c) => c.type === "number" || c.type === "integer");
  const text = cols.find((c) => c.type === "text");
  const date = cols.find((c) => c.type === "date");
  const out: string[] = [];
  if (num) out.push(`Sort by ${num.name} descending`);
  out.push("Remove duplicate rows");
  if (num) out.push(`Calculate average ${num.name}`);
  if (date) out.push(`Sort by ${date.name}`);
  if (text) out.push(`Filter ${text.name} = `);
  out.push("Trim extra spaces");
  return out.slice(0, 6);
}

function SuggestedActions({
  preview,
  onPick,
}: {
  preview: InspectResult | null;
  onPick: (text: string) => void;
}) {
  const suggestions = suggestionsFor(preview);
  if (suggestions.length === 0) return null;
  return (
    <div className="rounded-2xl border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">Suggested actions</p>
      <div className="flex flex-wrap gap-2">
        {suggestions.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            className="rounded-full border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs font-medium text-indigo-700 transition hover:border-indigo-400 hover:bg-indigo-100 active:scale-95 dark:border-indigo-900 dark:bg-indigo-950/40 dark:text-indigo-300"
          >
            {s}
          </button>
        ))}
      </div>
      <p className="mt-2 text-[10px] text-zinc-400">Click one to fill the box, then edit or press Run.</p>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="mx-auto mt-16 max-w-sm text-center">
      <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 text-white shadow-lg shadow-indigo-500/30">
        <SheetIcon className="h-6 w-6" />
      </div>
      <p className="text-sm font-medium">Upload spreadsheets, then describe what you want.</p>
      <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
        Use the upload button at the bottom. Then sort, filter, merge, look up, or add formulas —
        in any language — and download the result.
      </p>
    </div>
  );
}

// A CONSOLIDATED overview of the whole session (not a repeat of the center cards):
// a timeline of the steps run, every live formula used, and one action on the final
// result. The full per-step detail lives in the center transcript.
function SummaryPanel({
  messages,
  onContinue,
  onDownload,
}: {
  messages: Message[];
  onContinue: (r: OkResult) => void;
  onDownload: (r: OkResult) => void;
}) {
  // Group messages into turns (a user instruction + the assistant outputs after it).
  const turns: { instruction: string; outputs: Extract<Message, { role: "assistant" }>[] }[] = [];
  for (const m of messages) {
    if (m.role === "user") turns.push({ instruction: m.text, outputs: [] });
    else if (turns.length) turns[turns.length - 1].outputs.push(m);
    else turns.push({ instruction: "", outputs: [m] });
  }

  type Tone = "ok" | "ask" | "info" | "error";
  const steps: { label: string; detail: string; tone: Tone; formulas: string[] }[] = [];
  let latest: OkResult | null = null;

  for (const t of turns) {
    if (t.outputs.length === 0) continue;
    let detail = "";
    let tone: Tone = "info";
    const stepFormulas: string[] = [];
    for (const o of t.outputs) {
      if (o.kind === "result") {
        tone = "ok";
        detail = `${o.result.row_count.toLocaleString()} rows`;
        latest = o.result;
        for (const f of o.result.formulas ?? []) if (!stepFormulas.includes(f)) stepFormulas.push(f);
      }
    }
    if (tone !== "ok") {
      const last = t.outputs[t.outputs.length - 1];
      if (last.kind === "question") { tone = "ask"; detail = "asked a question"; }
      else if (last.kind === "error") { tone = "error"; detail = "couldn’t finish"; }
      else detail = "answered";
    }
    const label = t.instruction || (t.outputs[0].kind === "info" ? t.outputs[0].text : "Step");
    steps.push({ label, detail, tone, formulas: stepFormulas });
  }

  if (steps.length === 0) {
    return (
      <p className="text-xs text-zinc-400">
        A summary of this session — the steps you ran and any live formulas — appears here.
      </p>
    );
  }

  const dot: Record<Tone, string> = {
    ok: "bg-green-500", ask: "bg-amber-500", info: "bg-zinc-400", error: "bg-red-500",
  };

  return (
    <div className="space-y-3">
      <p className="text-xs text-zinc-500 dark:text-zinc-400">
        {steps.length} step{steps.length === 1 ? "" : "s"}
        {latest ? ` · ${latest.row_count.toLocaleString()} rows now` : ""}
      </p>

      {/* timeline of what was done — each step's live formula shown right under it */}
      <ol className="space-y-2">
        {steps.map((s, i) => (
          <li key={i} className="flex gap-2">
            <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${dot[s.tone]}`} />
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium text-zinc-700 dark:text-zinc-200" title={s.label}>
                {i + 1}. {s.label}
              </div>
              {s.detail && <div className="text-[10px] text-zinc-400">{s.detail}</div>}
              {s.formulas.length > 0 && (
                <div className="mt-1 space-y-0.5">
                  {s.formulas.map((f, j) => (
                    <code key={j} className="block overflow-x-auto rounded bg-zinc-900 px-2 py-1 text-[10px] text-indigo-200">
                      {f}
                    </code>
                  ))}
                  <p className="text-[9px] font-medium uppercase tracking-wide text-indigo-400">
                    Live &amp; editable in Excel
                  </p>
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>

      {/* a single action on the FINAL result (per-step buttons live in the center) */}
      {latest && canDownload(latest) && (
        <div className="space-y-1.5 border-t border-zinc-200 pt-2.5 dark:border-zinc-800">
          <p className="text-[10px] uppercase tracking-wide text-zinc-400">Final result</p>
          <button
            onClick={() => onDownload(latest!)}
            className="flex w-full items-center justify-center gap-1.5 rounded-md bg-zinc-900 px-2 py-1.5 text-[11px] font-semibold text-white transition hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900"
          >
            <DownloadIcon className="h-3 w-3" /> Download latest result
          </button>
          <button
            onClick={() => onContinue(latest!)}
            title="Start a fresh session using this result as the uploaded file"
            className="flex w-full items-center justify-center gap-1.5 rounded-md border border-indigo-300 bg-indigo-50 px-2 py-1.5 text-[11px] font-semibold text-indigo-700 transition hover:bg-indigo-100 dark:border-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-300"
          >
            <SheetIcon className="h-3 w-3" /> Use as a new file
          </button>
        </div>
      )}
    </div>
  );
}

function MessageBubble({
  message,
  onCopy,
  onRetry,
  onEdit,
  onContinue,
  onDownload,
}: {
  message: Message;
  onCopy: (t: string) => void;
  onRetry: () => void;
  onEdit: () => void;
  onContinue: (r: OkResult) => void;
  onDownload: (r: OkResult) => void;
}) {
  if (message.role === "user") {
    return (
      <div className="group flex flex-col items-end">
        <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-indigo-600 px-3.5 py-2 text-sm text-white">
          {message.files && message.files.length > 0 && (
            <div className="mb-1 flex flex-wrap justify-end gap-1">
              {message.files.map((n) => (
                <span key={n} className="inline-flex items-center gap-1 rounded bg-white/20 px-1.5 py-0.5 text-[11px]">
                  <FileIcon className="h-3 w-3" />
                  {n}
                </span>
              ))}
            </div>
          )}
          {message.text}
        </div>
        {/* per-message actions, with icons */}
        <div className="mt-1 flex gap-1 pr-1 opacity-0 transition group-hover:opacity-100">
          <IconBtn label="Copy" onClick={() => onCopy(message.text)}>
            <CopyIcon className="h-3.5 w-3.5" />
          </IconBtn>
          <IconBtn label="Edit" onClick={onEdit}>
            <PencilIcon className="h-3.5 w-3.5" />
          </IconBtn>
          <IconBtn label="Retry" onClick={onRetry}>
            <RetryIcon className="h-3.5 w-3.5" />
          </IconBtn>
        </div>
      </div>
    );
  }
  if (message.kind === "question") {
    return (
      <div className="flex justify-start">
        <div className="max-w-[90%] rounded-2xl rounded-bl-sm border border-amber-200 bg-amber-50 px-3.5 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <span className="mb-0.5 block text-xs font-semibold">Sumio asks</span>
          {message.text}
        </div>
      </div>
    );
  }
  if (message.kind === "info") {
    return (
      <div className="flex justify-start">
        <div className="max-w-[90%] rounded-2xl rounded-bl-sm border border-zinc-200 bg-zinc-50 px-3.5 py-2 text-sm text-zinc-700 dark:border-zinc-700 dark:bg-zinc-800/60 dark:text-zinc-200">
          {message.text}
        </div>
      </div>
    );
  }
  if (message.kind === "error") {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-3.5 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
        {message.text}
      </div>
    );
  }
  return <ResultCard result={message.result} onContinue={onContinue} onDownload={onDownload} />;
}

// A compact, scrollable table of the RESULT data, so the user can SEE the outcome
// (not just download it). Reused for each sheet of a multi-tab result.
function MiniTable({ table }: { table: InspectTable }) {
  const cols = table.columns;
  return (
    <div className="space-y-1">
      {table.name && (
        <p className="text-[11px] font-medium text-zinc-500 dark:text-zinc-400">
          <SheetIcon className="mr-1 inline h-3 w-3 text-indigo-500" />
          {table.name} · {table.row_count.toLocaleString()} rows · {cols.length} cols
        </p>
      )}
      <div className="max-h-60 overflow-auto rounded-lg border border-zinc-200 dark:border-zinc-800">
        <table className="w-full text-left text-[11px]">
          <thead className="sticky top-0 bg-zinc-50 dark:bg-zinc-800/80">
            <tr>
              {cols.map((c) => (
                <th key={c.name} className="whitespace-nowrap px-2 py-1 font-semibold text-zinc-600 dark:text-zinc-300" title={c.type}>
                  {c.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.sample_rows.map((row, ri) => (
              <tr key={ri} className="border-t border-zinc-100 dark:border-zinc-800">
                {cols.map((c) => (
                  <td key={c.name} className="whitespace-nowrap px-2 py-1 text-zinc-600 dark:text-zinc-300">
                    {fmtCell(row[c.name])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {table.truncated && (
        <p className="text-[10px] text-zinc-400">
          Showing the first {table.sample_rows.length} of {table.row_count.toLocaleString()} rows — download for all.
        </p>
      )}
    </div>
  );
}

function ResultPreview({ tables }: { tables?: InspectTable[] }) {
  const [open, setOpen] = useState(true);
  if (!tables || tables.length === 0) return null;
  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-zinc-400 transition hover:text-indigo-500"
      >
        <ChevronIcon className={`h-3 w-3 transition-transform ${open ? "rotate-90" : ""}`} />
        Preview of the result
      </button>
      {open && (
        <div className="space-y-2">
          {tables.map((t, i) => (
            <MiniTable key={i} table={t} />
          ))}
        </div>
      )}
    </div>
  );
}

function ResultCard({
  result,
  onContinue,
  onDownload,
}: {
  result: OkResult;
  onContinue?: (r: OkResult) => void;
  onDownload: (r: OkResult) => void;
}) {
  return (
    <div className={`space-y-3 rounded-2xl border bg-white p-4 shadow-sm transition hover:shadow-md dark:bg-zinc-900 ${result.partial ? "border-amber-300 dark:border-amber-800" : "border-zinc-200 dark:border-zinc-800"}`}>
      <p className="text-sm font-semibold">
        {result.partial ? "Here's what I managed" : "Here's what I did"}
      </p>
      <ul className="space-y-1.5 text-sm text-zinc-600 dark:text-zinc-400">
        {result.notes.map((note, i) => (
          <li key={i} className="flex gap-2">
            <CheckIcon className="mt-0.5 h-4 w-4 shrink-0 text-green-500" />
            <span>{note}</span>
          </li>
        ))}
      </ul>
      {result.warning && (
        <div className="flex gap-2 rounded-xl border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
          <WarnIcon className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{result.warning}</span>
        </div>
      )}
      {result.formulas && result.formulas.length > 0 && (
        <div className="space-y-1">
          {result.formulas.map((f, i) => (
            <code key={i} className="block overflow-x-auto rounded bg-zinc-900 px-2.5 py-1.5 text-xs text-indigo-200">{f}</code>
          ))}
        </div>
      )}
      {/* results summary: rows before → after, time, size (US-007) */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-400">
        <span>
          {typeof result.rows_before === "number" && result.rows_before !== result.row_count ? (
            <>
              <span className="text-zinc-500 dark:text-zinc-300">{result.rows_before.toLocaleString()}</span> →{" "}
              <span className="font-medium text-zinc-600 dark:text-zinc-200">{result.row_count.toLocaleString()}</span> rows
            </>
          ) : (
            <>{result.row_count.toLocaleString()} rows</>
          )}
        </span>
        {typeof result.elapsed_ms === "number" && <span>· {(result.elapsed_ms / 1000).toFixed(1)}s</span>}
        {typeof result.file_size === "number" && <span>· {formatSize(result.file_size)}</span>}
      </div>
      <ResultPreview tables={result.preview} />
      {canDownload(result) ? (
        <div className="flex flex-col gap-2 sm:flex-row">
          {onContinue && (
            <button
              onClick={() => onContinue(result)}
              title="Start a fresh session using this result as the uploaded file (your next step already builds on it automatically)"
              className="flex flex-1 items-center justify-center gap-2 rounded-xl border border-indigo-300 bg-indigo-50 px-4 py-2.5 text-sm font-semibold text-indigo-700 transition hover:bg-indigo-100 active:scale-[0.99] dark:border-indigo-800 dark:bg-indigo-950/40 dark:text-indigo-300"
            >
              <SheetIcon className="h-4 w-4" /> Use as a new file
            </button>
          )}
          <button
            onClick={() => onDownload(result)}
            className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-zinc-900 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-zinc-700 active:scale-[0.99] dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
          >
            <DownloadIcon className="h-4 w-4" /> Download{typeof result.file_size === "number" ? ` · ${formatSize(result.file_size)}` : ""}
          </button>
        </div>
      ) : (
        <p className="text-xs text-zinc-400">
          The file isn’t kept across reloads — re-run this step (Retry) to download it again.
        </p>
      )}
    </div>
  );
}

function BackendStatus({ health }: { health: HealthResult | null }) {
  const base = "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium";
  if (health === null)
    return <span className={`${base} bg-zinc-100 text-zinc-500 dark:bg-zinc-800`} title="Checking backend"><Dot className="bg-zinc-400" /></span>;
  if (!health.reachable)
    return <span className={`${base} bg-red-50 text-red-600 dark:bg-red-950/40 dark:text-red-400`} title="Backend offline"><Dot className="bg-red-500" /></span>;
  return <span className={`${base} bg-green-50 text-green-700 dark:bg-green-950/40 dark:text-green-400`} title="Backend connected"><Dot className="bg-green-500" /></span>;
}

function IconBtn({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-700 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
    >
      {children}
      {label}
    </button>
  );
}

// Inline editor shown in place of a user message when Editing it.
function EditBox({
  value,
  onChange,
  onSave,
  onCancel,
}: {
  value: string;
  onChange: (t: string) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  // Grow the edit box to fit the (possibly long) instruction being edited.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 220) + "px";
  }, [value]);
  return (
    <div className="flex flex-col items-end gap-1">
      <textarea
        ref={ref}
        autoFocus
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSave();
          }
          if (e.key === "Escape") onCancel();
        }}
        rows={2}
        className="max-h-[220px] w-[85%] resize-none overflow-y-auto rounded-2xl border border-indigo-300 bg-white px-3.5 py-2 text-sm outline-none focus:ring-2 focus:ring-indigo-500/20 dark:border-indigo-700 dark:bg-zinc-900"
      />
      <div className="flex gap-2 text-[11px]">
        <button onClick={onCancel} className="rounded px-2 py-0.5 text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800">
          Cancel
        </button>
        <button onClick={onSave} className="rounded bg-indigo-600 px-2 py-0.5 font-semibold text-white hover:bg-indigo-500">
          Save &amp; run
        </button>
      </div>
    </div>
  );
}

/* --- inline icons --- */
function Dot({ className }: { className?: string }) {
  return <span className={`h-2 w-2 rounded-full ${className ?? ""}`} />;
}
function CopyIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}
function PencilIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  );
}
function RetryIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" />
    </svg>
  );
}
function SunIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  );
}
function MoonIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />
    </svg>
  );
}
function SheetIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" /><path d="M3 9h18M3 15h18M9 3v18M15 3v18" />
    </svg>
  );
}
function UploadIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M17 8l-5-5-5 5M12 3v12" />
    </svg>
  );
}
function FileIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" />
    </svg>
  );
}
function XIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}
function PlusIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}
function CheckIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
function DownloadIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="M7 10l5 5 5-5M12 15V3" />
    </svg>
  );
}
function Spinner({ className }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className ?? ""}`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 0 1 8-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}
function ChevronIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 18l6-6-6-6" />
    </svg>
  );
}
function TrashIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m2 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
    </svg>
  );
}
function StopIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="currentColor">
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}
function WarnIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" /><path d="M12 9v4M12 17h.01" />
    </svg>
  );
}
function UndoIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 7v6h6" /><path d="M3 13a9 9 0 1 0 3-7.7L3 8" />
    </svg>
  );
}
