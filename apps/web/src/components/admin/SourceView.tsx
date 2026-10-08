"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { SourceDetail, SourceSyncItem } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { canEdit } from "@/lib/admin/roles";
import { primaryButton } from "./AdminShell";
import { type Problem, ProblemView, toProblem } from "./ProblemView";
import { SourceFiles } from "./SourceFiles";
import { formatTime, isActive, KIND_LABELS, SYNC_LABELS, syncColor } from "./sourceLabels";

// Пока синхронизация ждёт воркера или идёт — статус перезапрашивается.
const POLL_MS = 3000;

/** Источник: конфиг, запуск синхронизации, история с ошибками, файлы (docs/contracts.md §7). */
export function SourceView({
  session,
  tenantId,
  sourceId,
}: {
  session: AdminSession;
  tenantId: string;
  sourceId: string;
}) {
  const { api, me, expire } = session;
  const editable = canEdit(me, tenantId);
  const [source, setSource] = useState<SourceDetail | null>(null);
  const [syncs, setSyncs] = useState<SourceSyncItem[]>([]);
  const [full, setFull] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (e: unknown) => (e instanceof ApiError && e.status === 401 ? expire() : setProblem(toProblem(e))),
    [expire],
  );

  const reload = useCallback(
    () =>
      Promise.all([api.getSource(tenantId, sourceId), api.listSyncs(tenantId, sourceId)]).then(
        ([detail, history]) => {
          setSource(detail);
          setSyncs(history.syncs);
        },
        fail,
      ),
    [api, tenantId, sourceId, fail],
  );

  useEffect(() => {
    void reload();
  }, [reload]);

  const running = isActive(syncs[0] ?? null);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void reload(), POLL_MS);
    return () => clearInterval(timer);
  }, [running, reload]);

  async function sync() {
    setBusy(true);
    setProblem(null);
    try {
      await api.startSync(tenantId, sourceId, full);
      await reload();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  const back = (
    <Link href={`/admin/tenants/${tenantId}/sources`} className="text-sm text-chat-primary hover:underline">
      ← Источники
    </Link>
  );
  if (source === null) {
    return (
      <div className="space-y-3">
        {back}
        {problem ? <ProblemView problem={problem} title="Ошибка" /> : <p className="text-chat-muted">Загрузка…</p>}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        {back}
        <h1 className="mt-1 text-xl font-semibold">{source.name ?? source.id}</h1>
        <p className="text-sm text-chat-muted">
          {KIND_LABELS[source.kind]} · {source.documents} док. · {source.entities} зап.
          {source.status === "paused" && " · на паузе"}
        </p>
      </div>

      <section className="space-y-2">
        <h2 className="font-semibold">Конфиг</h2>
        {source.origin === "yaml" && (
          <p className="text-sm text-chat-muted">
            Источник из YAML тенанта: конфиг и файлы меняются в config/tenants и make seed.
          </p>
        )}
        <pre className="overflow-x-auto rounded-chat border border-chat-border bg-chat-surface p-3 text-sm">
          {source.config_yaml || "(настроек нет)"}
        </pre>
      </section>

      {(source.kind === "file" || source.kind === "table") && (
        <SourceFiles
          session={session}
          tenantId={tenantId}
          sourceId={sourceId}
          editable={editable && source.origin === "admin"}
        />
      )}

      <section className="space-y-3">
        <h2 className="font-semibold">Синхронизация</h2>
        {editable && (
          <div className="flex flex-wrap items-center gap-4">
            <button type="button" onClick={() => void sync()} disabled={busy || running} className={primaryButton}>
              Синхронизировать
            </button>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={full} onChange={(e) => setFull(e.target.checked)} />
              полная — удалит данные пропавших страниц и файлов
            </label>
          </div>
        )}
        {problem && <ProblemView problem={problem} title="Ошибка" />}
        {syncs.length === 0 ? (
          <p className="text-sm text-chat-muted">Синхронизаций ещё не было.</p>
        ) : (
          <div className="overflow-x-auto rounded-chat border border-chat-border bg-chat-surface">
            <table aria-label="История синхронизаций" className="w-full text-left text-sm">
              <thead className="text-chat-muted">
                <tr className="border-b border-chat-border">
                  <th className="px-3 py-2 font-medium">Статус</th>
                  <th className="px-3 py-2 font-medium">Запрошена</th>
                  <th className="px-3 py-2 font-medium">Закончена</th>
                  <th className="px-3 py-2 font-medium">Итог</th>
                </tr>
              </thead>
              <tbody>
                {syncs.map((s) => (
                  <SyncRow key={s.id} sync={s} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

function SyncRow({ sync }: { sync: SourceSyncItem }) {
  const stats = sync.stats;
  return (
    <tr className="border-b border-chat-border align-top last:border-0">
      <td className={`px-3 py-2 ${syncColor(sync.status)}`}>{SYNC_LABELS[sync.status]}</td>
      <td className="px-3 py-2">{formatTime(sync.created_at)}</td>
      <td className="px-3 py-2">{formatTime(sync.finished_at)}</td>
      <td className="px-3 py-2">
        {stats && (
          <span>
            {stats.incremental ? "изменения" : "полная"}: найдено {stats.discovered}, новых {stats.created},
            изменено {stats.updated}, без изменений {stats.unchanged}, удалено {stats.deleted}
            {stats.failed > 0 && <span className="text-chat-danger">, ошибок {stats.failed}</span>}
          </span>
        )}
        {sync.error && <p className="text-chat-danger">{sync.error}</p>}
        {stats && stats.errors.length > 0 && (
          <details className="mt-1">
            <summary className="cursor-pointer text-chat-muted">Ошибки элементов</summary>
            <ul className="mt-1 list-disc pl-5 font-mono text-xs">
              {stats.errors.map((e, i) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          </details>
        )}
      </td>
    </tr>
  );
}
