"use client";

import { useState } from "react";
import {
  processSpreadsheet,
  downloadResult,
  type ProcessResult,
} from "@/lib/api";

const EXAMPLES = [
  "Email ke basis pe duplicate rows hata do",
  "Add a Total column = Qty × Price",
  "Sort by Price descending",
];

export default function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [instruction, setInstruction] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ProcessResult | null>(null);

  const canSubmit = file && instruction.trim() && !loading;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file || !instruction.trim()) return;
    setLoading(true);
    setResult(null);
    const res = await processSpreadsheet(file, instruction.trim());
    setResult(res);
    setLoading(false);
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col gap-8 px-6 py-16">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Sumio</h1>
        <p className="text-zinc-500 dark:text-zinc-400">
          Upload a spreadsheet, describe what you want done in plain language
          (Hindi, English, Urdu, or any mix), and download the result.
        </p>
      </header>

      <form onSubmit={handleSubmit} className="space-y-5">
        {/* File upload */}
        <label className="block">
          <span className="mb-1.5 block text-sm font-medium">Spreadsheet</span>
          <input
            type="file"
            accept=".csv,.xlsx,.xls"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full cursor-pointer rounded-lg border border-zinc-300 text-sm file:mr-4 file:border-0 file:bg-zinc-900 file:px-4 file:py-2.5 file:text-white hover:file:bg-zinc-700 dark:border-zinc-700 dark:file:bg-zinc-100 dark:file:text-zinc-900"
          />
          {file && (
            <span className="mt-1 block text-xs text-zinc-500">{file.name}</span>
          )}
        </label>

        {/* Instruction */}
        <label className="block">
          <span className="mb-1.5 block text-sm font-medium">Instruction</span>
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            rows={3}
            placeholder="e.g. Email ke basis pe duplicate rows hata do"
            className="w-full resize-none rounded-lg border border-zinc-300 bg-transparent px-3 py-2.5 text-sm outline-none focus:border-zinc-900 dark:border-zinc-700 dark:focus:border-zinc-300"
          />
        </label>

        {/* Example chips */}
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((ex) => (
            <button
              key={ex}
              type="button"
              onClick={() => setInstruction(ex)}
              className="rounded-full border border-zinc-300 px-3 py-1 text-xs text-zinc-600 hover:border-zinc-900 hover:text-zinc-900 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-300 dark:hover:text-zinc-100"
            >
              {ex}
            </button>
          ))}
        </div>

        <button
          type="submit"
          disabled={!canSubmit}
          className="w-full rounded-lg bg-zinc-900 px-4 py-3 text-sm font-medium text-white transition disabled:cursor-not-allowed disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {loading ? "Working…" : "Run"}
        </button>
      </form>

      {result && <ResultPanel result={result} />}
    </main>
  );
}

function ResultPanel({ result }: { result: ProcessResult }) {
  if (result.status === "error") {
    return (
      <div className="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
        {result.error}
      </div>
    );
  }

  if (result.status === "clarify") {
    return (
      <div className="rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
        <p className="font-medium">One quick question:</p>
        <p className="mt-1">{result.clarification}</p>
      </div>
    );
  }

  return (
    <div className="space-y-4 rounded-lg border border-zinc-200 bg-zinc-50 p-5 dark:border-zinc-800 dark:bg-zinc-900/40">
      <div>
        <p className="text-sm font-medium">Here&apos;s what I did</p>
        <ul className="mt-2 space-y-1 text-sm text-zinc-600 dark:text-zinc-400">
          {result.notes.map((note, i) => (
            <li key={i} className="flex gap-2">
              <span className="text-green-600">✓</span>
              {note}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-xs text-zinc-500">
          {result.row_count} rows in the result.
        </p>
      </div>
      <button
        onClick={() => downloadResult(result)}
        className="rounded-lg bg-zinc-900 px-4 py-2.5 text-sm font-medium text-white dark:bg-zinc-100 dark:text-zinc-900"
      >
        Download {result.filename}
      </button>
    </div>
  );
}
