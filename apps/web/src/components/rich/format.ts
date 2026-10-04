import type { Price } from "@/contracts";

// Локаль пока фиксирована; язык по пользователю/конфигу — P6-04.
const LOCALE = "ru-RU";

export function formatPrice({ amount, currency }: Price): string {
  try {
    return new Intl.NumberFormat(LOCALE, {
      style: "currency",
      currency,
      maximumFractionDigits: Number.isInteger(amount) ? 0 : 2,
    }).format(amount);
  } catch {
    // Неизвестный Intl код валюты — показываем как есть.
    return `${amount} ${currency}`;
  }
}
