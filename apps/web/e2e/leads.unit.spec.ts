import { expect, test } from "@playwright/test";
import type { LeadSummary } from "@/contracts";
import { exportPeriod, mergeForms } from "../src/lib/admin/leads";

const lead = (form_key: string, form_title: string | null): LeadSummary => ({
  id: crypto.randomUUID(),
  number: 1,
  created_at: "2026-10-17T10:00:00Z",
  form_key,
  form_title,
  conversation_id: null,
  fields: [],
});

test("формы фильтра: без повторов, в порядке появления, к известным добавляются новые", () => {
  const first = mergeForms([], [lead("consultation", "Консультация"), lead("callback", "Звонок"), lead("consultation", "Другой заголовок")]);
  expect(first).toEqual([
    { key: "consultation", label: "Консультация" },
    { key: "callback", label: "Звонок" },
  ]);
  const known = mergeForms(first, [lead("order", "Заказ")]);
  expect(known.map((f) => f.key)).toEqual(["consultation", "callback", "order"]);
  // Отфильтрованная страница без старых форм их не убирает.
  expect(mergeForms(known, [])).toEqual(known);
});

test("формы фильтра: без заголовка — ключ, заголовок из более поздней заявки его заменяет", () => {
  const old = mergeForms([], [lead("consultation", null)]);
  expect(old).toEqual([{ key: "consultation", label: "consultation" }]);
  expect(mergeForms(old, [lead("consultation", "Консультация")])).toEqual([
    { key: "consultation", label: "Консультация" },
  ]);
  // Известные формы не меняются на месте.
  expect(old[0].label).toBe("consultation");
});

// Пояс задаётся явно: в CI с TZ=UTC местное и UTC-время совпали бы.
const withTimeZone = (zone: string, check: () => void) => () => {
  const saved = process.env.TZ;
  process.env.TZ = zone;
  try {
    check();
  } finally {
    if (saved === undefined) delete process.env.TZ;
    else process.env.TZ = saved;
  }
};

test(
  "период выгрузки: с начала дня «с» до начала дня после «по», в местном времени",
  withTimeZone("Europe/Moscow", () => {
    expect(exportPeriod("2026-10-01", "2026-10-31")).toEqual({
      from: "2026-09-30T21:00:00.000Z",
      to: "2026-10-31T21:00:00.000Z",
    });
    // Один день: «с» и «по» совпадают; конец года.
    expect(exportPeriod("2026-12-31", "2026-12-31")).toEqual({
      from: "2026-12-30T21:00:00.000Z",
      to: "2026-12-31T21:00:00.000Z",
    });
  }),
);

test(
  "период выгрузки: день перевода часов — 25 часов, а не сутки по UTC",
  withTimeZone("Europe/Berlin", () => {
    const { from, to } = exportPeriod("2026-10-25", "2026-10-25");
    expect(from).toBe("2026-10-24T22:00:00.000Z");
    expect(Date.parse(to!) - Date.parse(from!)).toBe(25 * 3600_000);
  }),
);

test("период выгрузки: пустая дата — без границы, «с» позже «по» — ошибка", () => {
  expect(exportPeriod("", "")).toEqual({ from: null, to: null });
  expect(exportPeriod("2026-10-01", "").to).toBeNull();
  expect(exportPeriod("", "2026-10-01").from).toBeNull();
  expect(() => exportPeriod("2026-10-02", "2026-10-01")).toThrow("Дата «с» позже даты «по»");
  // Год из пяти цифр или 0050 (date-поле Chrome это позволяет) — ошибка, а не 1950 год.
  expect(() => exportPeriod("0050-01-01", "")).toThrow("Год периода — от 1970 до 9999");
  expect(() => exportPeriod("", "10000-01-01")).toThrow("Год периода — от 1970 до 9999");
});
