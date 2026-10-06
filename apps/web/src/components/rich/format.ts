import type { Price } from "@/contracts";

/** Цена в локали языка диалога (`useI18n().locale`). */
export function formatPrice({ amount, currency }: Price, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency,
      maximumFractionDigits: Number.isInteger(amount) ? 0 : 2,
    }).format(amount);
  } catch {
    // Неизвестный Intl код валюты — показываем как есть.
    return `${amount} ${currency}`;
  }
}
