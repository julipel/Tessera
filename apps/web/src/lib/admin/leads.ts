// Экран заявок (ADR-0045): период выгрузки CSV и список форм для фильтра.
import type { LeadSummary } from "@/contracts";

export interface FormOption {
  key: string;
  label: string;
}

/**
 * Формы для фильтра — из уже загруженных заявок: конфиг тенанта viewer не читает, а `leads`
 * не зависит от `tenants`. Новые формы добавляются к известным в порядке появления (от новых
 * заявок к старым); подпись — первый встреченный заголовок формы, иначе ключ (заявки до ADR-0045).
 */
export function mergeForms(known: readonly FormOption[], leads: readonly LeadSummary[]): FormOption[] {
  const forms = known.map((f) => ({ ...f }));
  for (const lead of leads) {
    const found = forms.find((f) => f.key === lead.form_key);
    if (!found) forms.push({ key: lead.form_key, label: lead.form_title ?? lead.form_key });
    else if (found.label === found.key && lead.form_title) found.label = lead.form_title;
  }
  return forms;
}

/**
 * Период выгрузки из дат `<input type="date">` (YYYY-MM-DD) в местном времени: `from` — с
 * начала дня «с», `to` — до начала дня после «по», то есть дата «по» входит. Пустая дата —
 * без границы. Год вне 1970…9999 или «с» позже «по» — Error.
 */
export function exportPeriod(fromDate: string, toDate: string): { from: string | null; to: string | null } {
  const from = fromDate ? localDay(fromDate, 0) : null;
  const to = toDate ? localDay(toDate, 1) : null;
  if (from && to && from >= to) throw new Error("Дата «с» позже даты «по»");
  return { from: from?.toISOString() ?? null, to: to?.toISOString() ?? null };
}

function localDay(date: string, shift: number): Date {
  const [year, month, day] = date.split("-").map(Number);
  if (!(year >= 1970 && year <= 9999)) throw new Error("Год периода — от 1970 до 9999");
  return new Date(year, month - 1, day + shift);
}
