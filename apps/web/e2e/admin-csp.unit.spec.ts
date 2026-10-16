import { expect, test } from "@playwright/test";
import { adminCsp, newNonce, originOf } from "../src/lib/admin/csp";

test("CSP админки: скрипты и стили — свои и с nonce, без фреймов и плагинов", () => {
  const csp = adminCsp("bm9uY2U=", "http://localhost:8000", false);

  expect(csp.split("; ")).toEqual([
    "default-src 'self'",
    "script-src 'self' 'nonce-bm9uY2U=' 'strict-dynamic'",
    "style-src 'self' 'nonce-bm9uY2U='",
    "connect-src 'self' http://localhost:8000",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ]);
  expect(csp).not.toContain("unsafe-inline");
  expect(csp).not.toContain("unsafe-eval");
});

test("CSP админки: послабления только в next dev; без адреса API — только свой origin", () => {
  const dev = adminCsp("n", null, true);
  expect(dev).toContain("script-src 'self' 'nonce-n' 'strict-dynamic' 'unsafe-eval'");
  // Оверлей next dev вставляет <style> без nonce; скрипты и в dev — только с nonce.
  expect(dev).toContain("style-src 'self' 'unsafe-inline';");
  expect(adminCsp("n", null, false)).toContain("connect-src 'self';");
});

test("nonce: 128 бит в base64, каждый раз новый", () => {
  const nonces = new Set(Array.from({ length: 50 }, newNonce));
  expect(nonces.size).toBe(50);
  for (const nonce of nonces) expect(nonce).toMatch(/^[A-Za-z0-9+/]{22}==$/);
});

test("origin API: из адреса, невалидный — null", () => {
  expect(originOf("https://example.com/v1/")).toBe("https://example.com");
  expect(originOf("http://localhost:8000")).toBe("http://localhost:8000");
  expect(originOf("not a url")).toBeNull();
  expect(originOf(undefined)).toBeNull();
});
