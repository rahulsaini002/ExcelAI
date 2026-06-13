// Thin client for the Sumio backend. Keeps the fetch + response shapes in one
// place so the UI just deals with typed results.

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type ProcessResult =
  | {
      status: "ok";
      explanation: string;
      notes: string[];
      row_count: number;
      filename: string;
      media_type: string;
      file_base64: string;
    }
  | { status: "clarify"; clarification: string }
  | { status: "error"; error: string };

export async function processSpreadsheet(
  file: File,
  instruction: string,
): Promise<ProcessResult> {
  const form = new FormData();
  form.append("file", file);
  form.append("instruction", instruction);

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/process`, { method: "POST", body: form });
  } catch {
    return {
      status: "error",
      error:
        "Couldn't reach the backend. Is it running on " + API_BASE + "?",
    };
  }

  const data = (await res.json()) as ProcessResult;
  return data;
}

// Turn the base64 payload from the backend into a browser download.
export function downloadResult(result: Extract<ProcessResult, { status: "ok" }>) {
  const bytes = Uint8Array.from(atob(result.file_base64), (c) =>
    c.charCodeAt(0),
  );
  const blob = new Blob([bytes], { type: result.media_type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = result.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
