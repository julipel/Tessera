import { expect, test } from "@playwright/test";
import { brandingStyle, logoUrl } from "../src/lib/branding";

test("токены → CSS-переменные темы чата", () => {
  expect(
    brandingStyle({ tokens: { primary: "#2E6B3F", radius: "0.5rem", font: "PT Serif" } }),
  ).toEqual({
    "--chat-primary": "#2E6B3F",
    "--chat-on-primary": "#ffffff",
    "--chat-radius": "0.5rem",
    "--chat-font": '"PT Serif", ui-sans-serif, system-ui, sans-serif',
  });
});

test("на светлом основном цвете — тёмный текст кнопок", () => {
  expect(brandingStyle({ tokens: { primary: "#FC0" } })).toEqual({
    "--chat-primary": "#FC0",
    "--chat-on-primary": "#1f2328",
  });
});

test("без конфига и токенов — нейтральная тема", () => {
  expect(brandingStyle(undefined)).toEqual({});
  expect(brandingStyle({})).toEqual({});
  expect(brandingStyle({ tokens: {} })).toEqual({});
});

test("значения не в формате токена в CSS не попадают", () => {
  expect(
    brandingStyle({
      tokens: {
        primary: "red; background: url(https://evil.example)",
        radius: "12px; color: red",
        font: 'Inter", serif; x: "',
      },
    }),
  ).toEqual({});
});

test("логотип: только http(s) или путь от корня", () => {
  expect(logoUrl({ logo_url: "https://cdn.example/logo.svg" })).toBe("https://cdn.example/logo.svg");
  expect(logoUrl({ logo_url: "/demo/garden.svg" })).toBe("/demo/garden.svg");
  expect(logoUrl({ logo_url: "javascript:alert(1)" })).toBeNull();
  expect(logoUrl({ logo_url: null })).toBeNull();
  expect(logoUrl(undefined)).toBeNull();
});
