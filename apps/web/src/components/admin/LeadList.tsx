"use client";

import Link from "next/link";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { LeadSummary } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import { type FormOption, exportPeriod, mergeForms } from "@/lib/admin/leads";
import type { AdminSession } from "@/lib/admin/useAdminSession";
import { tenantName } from "@/lib/admin/roles";
import { primaryButton, secondaryButton } from "./AdminShell";
import { type Problem, ProblemView, toProblem } from "./ProblemView";
import { formatTime } from "./sourceLabels";

const PAGE = 50;
const MAX_NUMBER = 2_147_483_647;
const inputClass = "rounded-chat border border-chat-border bg-chat-surface px-2 py-1 text-sm";

/** Заявки тенанта от новых к старым, страницами; фильтр по форме, поиск по номеру, CSV (ADR-0045). */
export function LeadList({ session, tenantId }: { session: AdminSession; tenantId: string }) {
  const { api, me, expire } = session;
  const [formKey, setFormKey] = useState("");
  const [number, setNumber] = useState<number | null>(null);
  const [numberInput, setNumberInput] = useState("");
  const [forms, setForms] = useState<FormOption[]>([]);
  const [items, setItems] = useState<LeadSummary[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const [exporting, setExporting] = useState(false);
  // Поколение фильтров: ответ «Показать ещё» на прежний фильтр в новый список не попадает.
  const generation = useRef(0);

  const fail = useCallback(
    (e: unknown) => (e instanceof ApiError && e.status === 401 ? expire() : setProblem(toProblem(e))),
    [expire],
  );

  // Первая страница — при открытии и смене фильтров (список сбрасывает обработчик фильтра).
  useEffect(() => {
    let stale = false;
    api
      .listLeads(tenantId, { formKey: formKey || null, number, limit: PAGE })
      .then((page) => {
        if (stale) return;
        setItems(page.leads);
        setNext(page.next_before);
        setForms((known) => mergeForms(known, page.leads));
      })
      .catch((e: unknown) => stale || fail(e));
    return () => {
      stale = true;
    };
  }, [api, tenantId, formKey, number, fail]);

  async function more(before: string) {
    const current = generation.current;
    setLoading(true);
    setProblem(null);
    try {
      const page = await api.listLeads(tenantId, { before, formKey: formKey || null, number, limit: PAGE });
      if (current !== generation.current) return;
      setItems((prev) => [...(prev ?? []), ...page.leads]);
      setNext(page.next_before);
      setForms((known) => mergeForms(known, page.leads));
    } catch (e) {
      if (current === generation.current) fail(e);
    } finally {
      setLoading(false);
    }
  }

  function filter(change: { formKey?: string; number?: number | null }) {
    if (change.formKey !== undefined) setFormKey(change.formKey);
    if (change.number !== undefined) setNumber(change.number);
    generation.current += 1;
    setItems(null);
    setNext(null);
    setProblem(null);
  }

  function search(e: FormEvent) {
    e.preventDefault();
    const value = Number(numberInput);
    if (!Number.isInteger(value) || value < 1 || value > MAX_NUMBER) {
      setProblem({ message: `Номер заявки — целое число от 1 до ${MAX_NUMBER}`, details: [] });
      return;
    }
    if (value !== number) filter({ number: value });
  }

  function reset() {
    setNumberInput("");
    if (number !== null) filter({ number: null });
  }

  async function download() {
    setProblem(null);
    let period: ReturnType<typeof exportPeriod>;
    try {
      period = exportPeriod(fromDate, toDate);
    } catch (e) {
      setProblem(toProblem(e));
      return;
    }
    setExporting(true);
    try {
      const blob = await api.exportLeads(tenantId, { ...period, formKey: formKey || null });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "leads.csv";
      document.body.append(link);
      link.click();
      link.remove();
      // Сразу после click браузер может ещё не забрать файл.
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      fail(e);
    } finally {
      setExporting(false);
    }
  }

  const name = tenantName(me, tenantId);
  const filtered = formKey !== "" || number !== null;
  return (
    <div className="space-y-6">
      <div>
        <Link href="/admin" className="text-sm text-chat-primary hover:underline">
          ← Тенанты
        </Link>
        <h1 className="mt-1 text-xl font-semibold">Заявки{name && ` · ${name}`}</h1>
      </div>
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm">
          Форма
          <select value={formKey} onChange={(e) => filter({ formKey: e.target.value })} className={inputClass}>
            <option value="">Все формы</option>
            {forms.map((f) => (
              <option key={f.key} value={f.key}>
                {f.label}
              </option>
            ))}
          </select>
        </label>
        <form role="search" aria-label="Поиск по номеру" onSubmit={search} className="flex items-end gap-2">
          <label className="flex flex-col gap-1 text-sm">
            Номер заявки
            <input
              type="number"
              min={1}
              max={MAX_NUMBER}
              value={numberInput}
              onChange={(e) => setNumberInput(e.target.value)}
              className={`${inputClass} w-28`}
            />
          </label>
          <button type="submit" className={secondaryButton}>
            Найти
          </button>
          {number !== null && (
            <button type="button" onClick={reset} className={secondaryButton}>
              Сбросить
            </button>
          )}
        </form>
      </div>
      <fieldset className="flex flex-wrap items-end gap-2 rounded-chat border border-chat-border p-3">
        <legend className="px-1 text-sm text-chat-muted">Выгрузка CSV (с учётом формы; даты — местное время, в файле — UTC)</legend>
        <label className="flex flex-col gap-1 text-sm">
          С
          <input type="date" value={fromDate} onChange={(e) => setFromDate(e.target.value)} className={inputClass} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          По
          <input type="date" value={toDate} onChange={(e) => setToDate(e.target.value)} className={inputClass} />
        </label>
        <button type="button" disabled={exporting} onClick={() => void download()} className={primaryButton}>
          Скачать CSV
        </button>
      </fieldset>
      {problem && <ProblemView problem={problem} title="Ошибка" />}
      {items === null ? (
        !problem && <p className="text-chat-muted">Загрузка…</p>
      ) : items.length === 0 ? (
        <p className="text-chat-muted">{filtered ? "Заявок не найдено." : "Заявок пока нет."}</p>
      ) : (
        <div className="overflow-x-auto rounded-chat border border-chat-border bg-chat-surface">
          <table aria-label="Заявки" className="w-full text-left text-sm">
            <thead className="text-chat-muted">
              <tr className="border-b border-chat-border">
                <th className="px-4 py-2 font-medium">№</th>
                <th className="px-4 py-2 font-medium">Создана</th>
                <th className="px-4 py-2 font-medium">Форма</th>
                <th className="px-4 py-2 font-medium">Поля</th>
                <th className="px-4 py-2 font-medium">Диалог</th>
              </tr>
            </thead>
            <tbody>
              {items.map((lead) => (
                <tr key={lead.id} className="border-b border-chat-border align-top last:border-0">
                  <td className="whitespace-nowrap px-4 py-2 font-medium">№ {lead.number}</td>
                  <td className="whitespace-nowrap px-4 py-2 text-chat-muted">{formatTime(lead.created_at)}</td>
                  <td className="px-4 py-2">{lead.form_title ?? lead.form_key}</td>
                  <td className="px-4 py-2">
                    <dl className="space-y-0.5">
                      {lead.fields.map((f) => (
                        <div key={f.name}>
                          <dt className="inline text-chat-muted">{f.label}: </dt>
                          <dd className="inline whitespace-pre-wrap break-words">{f.value}</dd>
                        </div>
                      ))}
                    </dl>
                  </td>
                  <td className="whitespace-nowrap px-4 py-2">
                    {lead.conversation_id ? (
                      <Link
                        href={`/admin/tenants/${tenantId}/conversations/${lead.conversation_id}`}
                        className="text-chat-primary hover:underline"
                      >
                        Открыть
                      </Link>
                    ) : (
                      <span className="text-chat-muted" title="Удалён по сроку хранения">
                        удалён
                      </span>
                    )}
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
