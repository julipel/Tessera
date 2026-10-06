import { expect, test } from "@playwright/test";
import { formatPrice } from "../src/components/rich/format";
import { LOCALES, MESSAGES, i18n } from "../src/lib/i18n";

test("словарь: у каждого языка все строки интерфейса, без пустых", () => {
  const keys = Object.keys(MESSAGES.ru).sort();
  for (const [language, messages] of Object.entries(MESSAGES)) {
    expect(Object.keys(messages).sort(), language).toEqual(keys);
    for (const [key, value] of Object.entries(messages)) expect(value, `${language}.${key}`).not.toBe("");
  }
  expect(Object.keys(LOCALES).sort()).toEqual(Object.keys(MESSAGES).sort());
});

test("язык интерфейса: из конфига, до конфига и для неизвестного — русский", () => {
  expect(i18n("en")).toMatchObject({ language: "en", locale: "en-US", t: { send: "Send" } });
  expect(i18n("sv").t.typing).toBe("Skriver…");
  expect(i18n(undefined)).toMatchObject({ language: "ru", locale: "ru-RU" });
  expect(i18n("de" as never).language).toBe("ru");
});

test("цена — в локали языка диалога", () => {
  const price = { amount: 4990, currency: "SEK" };
  // Разделитель групп в ru-RU — неразрывный пробел.
  expect(formatPrice(price, "ru-RU")).toMatch(/^4\s990\sSEK$/);
  expect(formatPrice(price, "en-US")).toMatch(/^SEK\s4,990$/);
  expect(formatPrice(price, "sv-SE")).toMatch(/^4\s990\skr$/);
  expect(formatPrice({ amount: 12.5, currency: "XXX1" }, "en-US")).toBe("12.5 XXX1");
});
