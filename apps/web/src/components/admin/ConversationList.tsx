"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { ConversationSummary } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { tenantName } from "@/lib/admin/roles";
import { secondaryButton } from "./AdminShell";
import { type Problem, ProblemView, toProblem } from "./ProblemView";
import { formatTime } from "./sourceLabels";

const PAGE = 50;

/** Диалоги тенанта от новых к старым, страницами; фильтр — с плохой оценкой (ADR-0038). */
export function ConversationList({ session, tenantId }: { session: AdminSession; tenantId: string }) {
  const { api, me, expire } = session;
  const [onlyBad, setOnlyBad] = useState(false);
  const [items, setItems] = useState<ConversationSummary[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);

  const fail = useCallback(
    (e: unknown) => (e instanceof ApiError && e.status === 401 ? expire() : setProblem(toProblem(e))),
    [expire],
  );

  const rating = onlyBad ? "bad" : null;

  // Первая страница — при открытии и смене фильтра (onlyBad сбрасывает список в обработчике).
  useEffect(() => {
    api
      .listConversations(tenantId, { rating, limit: PAGE })
      .then((page) => {
        setItems(page.conversations);
        setNext(page.next_before);
      })
      .catch(fail);
  }, [api, tenantId, rating, fail]);

  async function more(before: string) {
    setLoading(true);
    setProblem(null);
    try {
      const page = await api.listConversations(tenantId, { before, rating, limit: PAGE });
      setItems((prev) => [...(prev ?? []), ...page.conversations]);
      setNext(page.next_before);
    } catch (e) {
      fail(e);
    } finally {
      setLoading(false);
    }
  }

  function filter(bad: boolean) {
    setOnlyBad(bad);
    setItems(null);
    setNext(null);
    setProblem(null);
  }

  const name = tenantName(me, tenantId);
  return (
    <div className="space-y-6">
      <div>
        <Link href="/admin" className="text-sm text-chat-primary hover:underline">
          ← Тенанты
        </Link>
        <h1 className="mt-1 text-xl font-semibold">Диалоги{name && ` · ${name}`}</h1>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={onlyBad} onChange={(e) => filter(e.target.checked)} />
        Только с плохой оценкой
      </label>
      {problem && <ProblemView problem={problem} title="Ошибка" />}
      {items === null ? (
        !problem && <p className="text-chat-muted">Загрузка…</p>
      ) : items.length === 0 ? (
        <p className="text-chat-muted">{onlyBad ? "Диалогов с плохой оценкой нет." : "Диалогов пока нет."}</p>
      ) : (
        <div className="overflow-x-auto rounded-chat border border-chat-border bg-chat-surface">
          <table aria-label="Диалоги" className="w-full text-left text-sm">
            <thead className="text-chat-muted">
              <tr className="border-b border-chat-border">
                <th className="px-4 py-2 font-medium">Начало</th>
                <th className="px-4 py-2 font-medium">Первый вопрос</th>
                <th className="px-4 py-2 font-medium">Сообщений</th>
                <th className="px-4 py-2 font-medium">Последнее</th>
                <th className="px-4 py-2 font-medium">Оценки</th>
              </tr>
            </thead>
            <tbody>
              {items.map((c) => (
                <tr key={c.id} className="border-b border-chat-border last:border-0">
                  <td className="whitespace-nowrap px-4 py-2 text-chat-muted">{formatTime(c.created_at)}</td>
                  <td className="px-4 py-2">
                    <Link
                      href={`/admin/tenants/${tenantId}/conversations/${c.id}`}
                      className="text-chat-primary hover:underline"
                    >
                      {c.preview ?? "(без сообщений)"}
                    </Link>
                  </td>
                  <td className="px-4 py-2">{c.messages}</td>
                  <td className="whitespace-nowrap px-4 py-2 text-chat-muted">{formatTime(c.last_message_at)}</td>
                  <td className="whitespace-nowrap px-4 py-2">
                    <RatingCounts good={c.ratings.good} bad={c.ratings.bad} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {next && (
        <button type="button" disabled={loading} onClick={() => void more(next)} className={secondaryButton}>
          Показать ещё
        </button>
      )}
    </div>
  );
}

export function RatingCounts({ good, bad }: { good: number; bad: number }) {
  if (good === 0 && bad === 0) return <span className="text-chat-muted">—</span>;
  return (
    <>
      {good > 0 && <span title="Хороших ответов">👍 {good}</span>}
      {bad > 0 && (
        <span title="Плохих ответов" className="ml-2 text-chat-danger">
          👎 {bad}
        </span>
      )}
    </>
  );
}
