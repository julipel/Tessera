"use client";

import { type ChangeEvent, useCallback, useEffect, useState } from "react";
import type { SourceFile } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { secondaryButton } from "./AdminShell";
import { type Problem, ProblemView, toProblem } from "./ProblemView";
import { formatTime } from "./sourceLabels";

/** Файлы источника file/table. Загрузка и удаление — у источника из админки и роли editor. */
export function SourceFiles({
  session,
  tenantId,
  sourceId,
  editable,
}: {
  session: AdminSession;
  tenantId: string;
  sourceId: string;
  editable: boolean;
}) {
  const { api, expire } = session;
  const [files, setFiles] = useState<SourceFile[] | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (e: unknown) => (e instanceof ApiError && e.status === 401 ? expire() : setProblem(toProblem(e))),
    [expire],
  );
  const reload = useCallback(
    () => api.listFiles(tenantId, sourceId).then(({ files }) => setFiles(files), fail),
    [api, tenantId, sourceId, fail],
  );

  useEffect(() => {
    void reload();
  }, [reload]);

  async function run(action: () => Promise<string>) {
    setBusy(true);
    setProblem(null);
    setNotice(null);
    try {
      setNotice(await action());
    } catch (e) {
      fail(e);
    } finally {
      await reload();
      setBusy(false);
    }
  }

  function upload(e: ChangeEvent<HTMLInputElement>) {
    const chosen = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (chosen.length === 0) return;
    void run(async () => {
      // По одному: ошибка файла останавливает загрузку, загруженные до неё остаются.
      for (const file of chosen) {
        try {
          await api.uploadFile(tenantId, sourceId, file);
        } catch (err) {
          if (err instanceof ApiError) throw new ApiError(err.status, { ...err.body, message: `${file.name}: ${err.message}` });
          throw err;
        }
      }
      return `Загружено файлов: ${chosen.length}. Запустите синхронизацию, чтобы они попали в поиск.`;
    });
  }

  function remove(name: string) {
    if (!window.confirm(`Удалить ${name}?`)) return;
    void run(async () => {
      await api.deleteFile(tenantId, sourceId, name);
      return `${name} удалён. Его данные уйдут из поиска после полной синхронизации.`;
    });
  }

  return (
    <section className="space-y-3">
      <h2 className="font-semibold">Файлы</h2>
      {files === null ? (
        !problem && <p className="text-sm text-chat-muted">Загрузка…</p>
      ) : files.length === 0 ? (
        <p className="text-sm text-chat-muted">Файлов нет.</p>
      ) : (
        <ul aria-label="Файлы источника" className="divide-y divide-chat-border rounded-chat border border-chat-border bg-chat-surface">
          {files.map((f) => (
            <li key={f.name} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
              <span className="flex-1 font-medium">{f.name}</span>
              <span className="text-chat-muted">{formatSize(f.size)}</span>
              <span className="text-chat-muted">{formatTime(f.modified_at)}</span>
              {editable && (
                <button
                  type="button"
                  onClick={() => remove(f.name)}
                  disabled={busy}
                  aria-label={`Удалить ${f.name}`}
                  className={secondaryButton}
                >
                  Удалить
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <label className="block text-sm">
          Загрузить файлы (до 20 МБ; с тем же именем — заменяется)
          <input type="file" multiple onChange={upload} disabled={busy} className="mt-1 block text-sm" />
        </label>
      )}
      {notice && (
        <p role="status" className="text-sm text-chat-muted">
          {notice}
        </p>
      )}
      {problem && <ProblemView problem={problem} title="Ошибка" />}
    </section>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}
