"use client";

import { useCallback, useEffect, useState } from "react";
import type { AgentConfigVersionDetail, AgentConfigVersionSummary } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { primaryButton, secondaryButton } from "./AdminShell";
import { type Problem, ProblemView, toProblem } from "./ProblemView";

const STATUS_LABELS = { draft: "черновик", active: "активна", archived: "архив" } as const;

/**
 * Версии AgentConfig тенанта (docs/contracts.md §7): YAML выбранной версии в редакторе,
 * сохранение — новый черновик, активация черновика или архивной версии (откат).
 */
export function ConfigEditor({ session, tenantId }: { session: AdminSession; tenantId: string }) {
  const { api, expire } = session;
  const [versions, setVersions] = useState<AgentConfigVersionSummary[] | null>(null);
  const [current, setCurrent] = useState<AgentConfigVersionDetail | null>(null);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<Problem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const dirty = current !== null && text !== current.yaml;

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) return expire();
      if (e instanceof ApiError && e.status === 403) {
        return setProblem({ message: "Нет доступа: конфиг меняет роль editor", details: [] });
      }
      setProblem(toProblem(e));
    },
    [expire],
  );

  const show = useCallback((version: AgentConfigVersionDetail) => {
    setCurrent(version);
    setText(version.yaml);
  }, []);

  const apply = useCallback(
    ({ versions, open }: Loaded) => {
      setVersions(versions);
      if (open) show(open);
    },
    [show],
  );

  useEffect(() => {
    load(api, tenantId).then(apply).catch(fail);
  }, [api, tenantId, apply, fail]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setProblem(null);
    setNotice(null);
    try {
      await action();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  function open(id: string) {
    if (dirty && !window.confirm("Несохранённые правки пропадут. Открыть другую версию?")) return;
    void run(async () => show(await api.getConfig(tenantId, id)));
  }

  function save() {
    void run(async () => {
      const draft = await api.createConfig(tenantId, text);
      apply(await load(api, tenantId, draft.id));
      setNotice(`Сохранено: черновик v${draft.version}. Чат работает на активной версии, пока черновик не активирован.`);
    });
  }

  function activate() {
    if (!current) return;
    const rollback = current.status === "archived";
    const question = rollback
      ? `Откатить на v${current.version}? Новые диалоги начнутся на ней.`
      : `Активировать v${current.version}? Новые диалоги начнутся на ней.`;
    if (!window.confirm(question)) return;
    void run(async () => {
      const active = await api.activateConfig(tenantId, current.id);
      apply(await load(api, tenantId, active.id));
      setNotice(`Активна v${active.version}. Начатые диалоги остаются на своей версии.`);
    });
  }

  if (versions === null) {
    return problem ? <ProblemView problem={problem} title="Конфиг не прошёл проверку" /> : <p className="text-chat-muted">Загрузка…</p>;
  }

  return (
    <div className="grid gap-6 md:grid-cols-[14rem_1fr]">
      <nav aria-label="Версии конфига">
        <h1 className="mb-3 text-xl font-semibold">Конфиг агента</h1>
        <ul className="space-y-1">
          {versions.map((v) => (
            <li key={v.id}>
              <button
                type="button"
                onClick={() => open(v.id)}
                aria-current={v.id === current?.id}
                data-status={v.status}
                className={`flex w-full items-center justify-between rounded-chat px-3 py-2 text-left text-sm ${
                  v.id === current?.id ? "bg-chat-surface ring-1 ring-chat-primary" : "hover:bg-chat-surface"
                }`}
              >
                <span className="font-medium">v{v.version}</span>
                <span className={v.status === "active" ? "text-chat-primary" : "text-chat-muted"}>
                  {STATUS_LABELS[v.status]}
                </span>
              </button>
              <div className="px-3 text-xs text-chat-muted">{new Date(v.created_at).toLocaleString("ru-RU")}</div>
            </li>
          ))}
        </ul>
      </nav>

      <section className="min-w-0 space-y-3">
        {current && (
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="flex-1 font-semibold">
              v{current.version} · {STATUS_LABELS[current.status]}
              {dirty && <span className="ml-2 text-sm font-normal text-chat-muted">изменено</span>}
            </h2>
            {current.status !== "active" && !dirty && (
              <button type="button" onClick={activate} disabled={busy} className={secondaryButton}>
                {current.status === "archived" ? "Откатить на эту версию" : "Активировать"}
              </button>
            )}
            <button type="button" onClick={save} disabled={busy || !dirty} className={primaryButton}>
              Сохранить черновик
            </button>
          </div>
        )}
        <textarea
          aria-label="YAML конфига"
          value={text}
          onChange={(e) => setText(e.target.value)}
          spellCheck={false}
          className="h-[70dvh] w-full resize-y rounded-chat border border-chat-border bg-chat-surface p-3 font-mono text-sm focus:outline-2 focus:outline-chat-primary"
        />
        {notice && (
          <p role="status" className="text-sm text-chat-muted">
            {notice}
          </p>
        )}
        {problem && <ProblemView problem={problem} title="Конфиг не прошёл проверку" />}
      </section>
    </div>
  );
}

interface Loaded {
  versions: AgentConfigVersionSummary[];
  open: AgentConfigVersionDetail | null;
}

/** Список версий и версия `openId` (по умолчанию — активная). */
async function load(api: AdminSession["api"], tenantId: string, openId?: string): Promise<Loaded> {
  const { versions } = await api.listConfigs(tenantId);
  const target = versions.find((v) => v.id === openId) ?? versions.find((v) => v.status === "active");
  return { versions, open: target ? await api.getConfig(tenantId, target.id) : null };
}
