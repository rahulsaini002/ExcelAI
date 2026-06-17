// Thin client for the Sumio backend. Keeps the fetch + response shapes in one
// place so the UI just deals with typed results.

// Default to 127.0.0.1 (not "localhost"): on Windows "localhost" can resolve to
// IPv6 (::1) first, but the backend listens on IPv4 — using the IP avoids
// "couldn't reach the backend". Override with NEXT_PUBLIC_API_BASE_URL if needed.
const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

// Result of a backend health check. Either we reached it (and it told us its
// status + model), or we couldn't connect at all.
export type HealthResult =
  | { reachable: true; status: string; model: string }
  | { reachable: false; error: string };

// Calls the backend's /health route. Used on page load to show the user whether
// the backend is up before they try to do anything.
export async function checkHealth(): Promise<HealthResult> {
  try {
    const res = await fetch(`${API_BASE}/health`);
    const data = (await res.json()) as { status: string; model: string };
    return { reachable: true, status: data.status, model: data.model };
  } catch {
    return {
      reachable: false,
      error: `Couldn't reach the backend at ${API_BASE}. Is it running?`,
    };
  }
}

// Structure preview (shown after upload, before any operation).
export type InspectColumn = { name: string; type: string };
export type InspectTable = {
  name: string;
  row_count: number;
  columns: InspectColumn[];
  sample_rows: Record<string, unknown>[];
  note?: string | null;
  truncated?: boolean;
};

export type ProcessResult =
  | {
      status: "ok";
      explanation: string;
      notes: string[];
      formulas?: string[];
      row_count: number;
      rows_before?: number | null; // input row count (for the before→after summary)
      preview?: InspectTable[];
      actions?: string[]; // operation types in this step (for the session title)
      ai_title?: string | null; // AI-suggested short title for the task
      partial?: boolean; // a later step of a multi-step plan failed
      warning?: string | null; // which step failed and why (when partial)
      filename: string;
      media_type: string;
      file_size?: number; // bytes of the result file
      elapsed_ms?: number; // server processing time
      download_id?: string; // stream the file from /download/{id} (large files)
      file_base64?: string | null; // inline bytes, only for small results
    }
  | { status: "clarify"; clarification: string }
  | { status: "message"; message: string }
  | { status: "error"; error: string };
export type InspectResult =
  | { status: "ok"; tables: InspectTable[] }
  | { status: "error"; error: string };

// Reads uploaded file(s) and returns their structure (sheets, columns + types,
// row count, sample rows) without running any operation.
export async function inspectFiles(files: File[]): Promise<InspectResult> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/inspect`, { method: "POST", body: form });
  } catch {
    return { status: "error", error: `Couldn't reach the backend at ${API_BASE}.` };
  }
  return (await res.json()) as InspectResult;
}

export class CancelledError extends Error {}

// Undo the last step on the server (pops the session's last state). Returns whether
// it worked and how many steps remain.
export async function undoOp(
  sessionId: string,
): Promise<{ ok: boolean; steps_remaining?: number; row_count?: number; primary?: string; error?: string }> {
  const form = new FormData();
  form.append("session_id", sessionId);
  try {
    const res = await fetch(`${API_BASE}/undo`, { method: "POST", body: form });
    const data = await res.json();
    if (data.status === "ok")
      return { ok: true, steps_remaining: data.steps_remaining, row_count: data.row_count, primary: data.primary };
    return { ok: false, error: data.error };
  } catch {
    return { ok: false, error: "Couldn't reach the backend." };
  }
}

export async function processSpreadsheet(
  files: File[],
  instruction: string,
  sessionId: string,
  rewind = -1,
  history = "",
  signal?: AbortSignal,
): Promise<ProcessResult> {
  const form = new FormData();
  // The backend expects one or more parts named "files" (omitted on follow-ups,
  // where the backend reuses the session's current data).
  for (const file of files) form.append("files", file);
  form.append("instruction", instruction);
  form.append("session_id", sessionId);
  // rewind >= 0 re-runs an earlier step (Retry/Edit); -1 means "continue from latest".
  form.append("rewind", String(rewind));
  // Recent conversation so the backend can interpret follow-up instructions.
  form.append("history", history);

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/process`, { method: "POST", body: form, signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === "AbortError") throw new CancelledError();
    return {
      status: "error",
      error:
        "Couldn't reach the backend. Is it running on " + API_BASE + "?",
    };
  }

  const data = (await res.json()) as ProcessResult;
  return data;
}

type OkResult = Extract<ProcessResult, { status: "ok" }>;

export function downloadUrl(id: string): string {
  return `${API_BASE}/download/${id}`;
}

// True if this result can still be downloaded (inline bytes OR a live server file).
export function canDownload(result: OkResult): boolean {
  return Boolean(result.file_base64 || result.download_id);
}

function triggerDownload(href: string, filename: string, revoke = false) {
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  if (revoke) URL.revokeObjectURL(href);
}

// Download a result. Inline bytes (small results) become a same-origin blob. For
// server-backed results (e.g. a restored session from history) we FIRST check the
// file still exists, so a gone/expired file gives a clear error instead of silently
// navigating the tab to a 404 — then we stream it straight to disk (no memory).
// Throws with a user-friendly message when the file can't be downloaded.
export async function downloadResult(result: OkResult): Promise<void> {
  if (result.file_base64) {
    const bytes = Uint8Array.from(atob(result.file_base64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: result.media_type }));
    triggerDownload(url, result.filename, true);
    return;
  }
  if (result.download_id) {
    const url = downloadUrl(result.download_id);
    let exists = false;
    try {
      exists = (await fetch(url, { method: "HEAD" })).ok;
    } catch {
      throw new Error("Couldn't reach the server to download this file. Is the backend running?");
    }
    if (!exists) {
      throw new Error("This file is no longer on the server — use Retry on this step to regenerate it.");
    }
    triggerDownload(url, result.filename); // streams to disk via the server's attachment header
    return;
  }
  throw new Error("This result isn’t available anymore — use Retry on this step to regenerate it.");
}

// Turn a generated result into a File (for "Continue with this file"). Uses the
// inline bytes when present, otherwise fetches the streamed file once.
export async function resultToFile(result: OkResult): Promise<File> {
  if (result.file_base64) {
    const bytes = Uint8Array.from(atob(result.file_base64), (c) => c.charCodeAt(0));
    return new File([bytes], result.filename, { type: result.media_type });
  }
  if (result.download_id) {
    const res = await fetch(downloadUrl(result.download_id));
    if (!res.ok) throw new Error("This result has expired on the server.");
    const blob = await res.blob();
    return new File([blob], result.filename, { type: result.media_type });
  }
  throw new Error("This result is no longer available — re-run the step.");
}
