"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, useCallback, useEffect, useState } from "react";
import type { CreateSourceRequest, SourceSummary } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { canEdit, tenantName } from "@/lib/admin/roles";
import { primaryButton } from "./AdminShell";
import { type Problem, ProblemView, toProblem } from "./ProblemView";
import { formatTime, KIND_LABELS, SYNC_LABELS, syncColor } from "./sourceLabels";

type AdminKind = Extract<CreateSourceRequest["kind"], "website" | "file" | "table">;

// Шаблоны конфига при выборе вида (docs/architecture.md §8): у file настроек нет.
const TEMPLATES: Record<AdminKind, string> = {
  website: "start_urls:\n  - https://example.ru/\nmax_pages: 200\n",
  file: "",
  table: "entity_type: product\ncolumns:\n  external_id: Артикул\n  title: Название\n  price: Цена\n",
};

/** Источники тенанта: список со статусами синхронизации и добавление (docs/contracts.md §7). */
export function SourceList({ session, tenantId }: { session: AdminSession; tenantId: string }) {
  const { api, me, expire } = session;
  const editable = canEdit(me, tenantId);
  const [sources, setSources] = useState<SourceSummary[] | null>(null);
  const [problem, setProblem] = useState<Problem | null>(null);

  const fail = useCallback(
    (e: unknown) => (e instanceof ApiError && e.status === 401 ? expire() : setProblem(toProblem(e))),
    [expire],
  );

  useEffect(() => {
    api
      .listSources(tenantId)
      .then(({ sources }) => setSources(sources))
      .catch(fail);
  }, [api, tenantId, fail]);

  return (
    <div className="space-y-6">
      <div>
        <Link href="/admin" className="text-sm text-chat-primary hover:underline">
          ← Тенанты
        </Link>
        <h1 className="mt-1 text-xl font-semibold">Источники{tenantName(me, tenantId) && ` · ${tenantName(me, tenantId)}`}</h1>
      </div>
      {problem && <ProblemView problem={problem} title="Ошибка" />}
      {sources === null ? (
        !problem && <p className="text-chat-muted">Загрузка…</p>
      ) : sources.length === 0 ? (
        <p className="text-chat-muted">Источников пока нет.</p>
      ) : (
        <div className="overflow-x-auto rounded-chat border border-chat-border bg-chat-surface">
          <table className="w-full text-left text-sm">
            <thead className="text-chat-muted">
              <tr className="border-b border-chat-border">
                <th className="px-4 py-2 font-medium">Имя</th>
                <th className="px-4 py-2 font-medium">Вид</th>
                <th className="px-4 py-2 font-medium">Данные</th>
                <th className="px-4 py-2 font-medium">Синхронизация</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id} className="border-b border-chat-border last:border-0">
                  <td className="px-4 py-2">
                    <Link
                      href={`/admin/tenants/${tenantId}/sources/${s.id}`}
                      className="font-medium text-chat-primary hover:underline"
                    >
                      {s.name ?? s.id}
                    </Link>
                    {s.origin === "yaml" && <span className="ml-2 text-xs text-chat-muted">из YAML</span>}
                  </td>
                  <td className="px-4 py-2">{KIND_LABELS[s.kind]}</td>
                  <td className="px-4 py-2">
                    {s.documents} док. · {s.entities} зап.
                  </td>
                  <td className="px-4 py-2">
                    {s.last_sync ? (
                      <>
                        <span className={syncColor(s.last_sync.status)}>{SYNC_LABELS[s.last_sync.status]}</span>
                        <span className="ml-2 text-chat-muted">{formatTime(s.last_sync.created_at)}</span>
                        {!!s.last_sync.stats?.failed && (
                          <span className="ml-2 text-chat-danger">ошибок: {s.last_sync.stats.failed}</span>
                        )}
                      </>
                    ) : (
                      <span className="text-chat-muted">не было</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {editable && <NewSourceForm session={session} tenantId={tenantId} />}
    </div>
  );
}

function NewSourceForm({ session, tenantId }: { session: AdminSession; tenantId: string }) {
  const { api, expire } = session;
  const router = useRouter();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<AdminKind>("website");
  const [config, setConfig] = useState(TEMPLATES.website);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);

  function changeKind(next: AdminKind) {
    // Шаблон подставляется, только если конфиг не правили.
    if (config === TEMPLATES[kind]) setConfig(TEMPLATES[next]);
    setKind(next);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      const created = await api.createSource(tenantId, { name, kind, config_yaml: config });
      router.push(`/admin/tenants/${tenantId}/sources/${created.id}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return expire();
      setProblem(toProblem(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3 rounded-chat border border-chat-border bg-chat-surface p-4">
      <h2 className="font-semibold">Добавить источник</h2>
      <p className="text-sm text-chat-muted">
        Сайт, файлы (PDF, DOCX, MD, TXT) или таблицы (CSV, XLSX). HTTP API и базы данных подключаются в YAML
        тенанта — им нужны секреты платформы.
      </p>
      <div className="flex flex-wrap gap-3">
        <label className="text-sm">
          Имя
          <input
            required
            pattern="[a-z0-9][a-z0-9_\-]*"
            maxLength={64}
            title="Строчные латинские буквы, цифры, - и _"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={input}
          />
        </label>
        <label className="text-sm">
          Вид
          <select value={kind} onChange={(e) => changeKind(e.target.value as AdminKind)} className={input}>
            {(Object.keys(TEMPLATES) as AdminKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="block text-sm">
        Конфиг (YAML)
        <textarea
          value={config}
          onChange={(e) => setConfig(e.target.value)}
          spellCheck={false}
          placeholder={kind === "file" ? "У файлов настроек нет" : undefined}
          className={`${input} h-40 w-full font-mono`}
        />
      </label>
      {problem && <ProblemView problem={problem} title="Источник не прошёл проверку" />}
      <button type="submit" disabled={busy} className={primaryButton}>
        Добавить
      </button>
    </form>
  );
}

const input =
  "mt-1 block rounded-chat border border-chat-border bg-chat-bg px-3 py-2 focus:outline-2 focus:outline-chat-primary";
