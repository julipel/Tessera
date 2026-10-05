import { expect, test } from "@playwright/test";
import { fetchAllowedOrigins, frameAncestors } from "../src/lib/widget/csp";

test("frame-ancestors: origin из allowed_origins, без повторов", () => {
  expect(frameAncestors(["https://shop.example", "http://localhost:3000", "https://shop.example"])).toBe(
    "frame-ancestors https://shop.example http://localhost:3000",
  );
});

test("frame-ancestors: пусто или не список — встраивание запрещено", () => {
  expect(frameAncestors([])).toBe("frame-ancestors 'none'");
  expect(frameAncestors(undefined)).toBe("frame-ancestors 'none'");
  expect(frameAncestors("https://shop.example")).toBe("frame-ancestors 'none'");
});

test("frame-ancestors: значения не в формате origin в заголовок не попадают", () => {
  expect(
    frameAncestors([
      "https://shop.example; script-src *",
      "https://shop.example/path",
      "https://shop.example/",
      "*",
      "https://*.example",
      "HTTPS://SHOP.EXAMPLE",
      "javascript:alert(1)",
      "https://ok.example:8443",
      42,
    ]),
  ).toBe("frame-ancestors https://ok.example:8443");
  expect(frameAncestors(["'self'", "data:"])).toBe("frame-ancestors 'none'");
});

const API = "http://api.test";
const respond = (status: number, body: unknown) =>
  (async () => new Response(JSON.stringify(body), { status })) as unknown as typeof fetch;

test("allowed_origins: запрос с ключом виджета к /v1/public/widget", async () => {
  let seen: { url: string; key: string | null } | null = null;
  const fetchImpl = (async (url: URL, init: RequestInit) => {
    seen = { url: String(url), key: new Headers(init.headers).get("X-Widget-Key") };
    return new Response(JSON.stringify({ allowed_origins: ["https://shop.example"] }));
  }) as unknown as typeof fetch;

  expect(await fetchAllowedOrigins(API, "wk_1", fetchImpl)).toEqual(["https://shop.example"]);
  expect(seen).toEqual({ url: `${API}/v1/public/widget`, key: "wk_1" });
});

test("allowed_origins: при любой ошибке — пустой список (закрыто по умолчанию)", async () => {
  const unauthorized = { error: { code: "unauthorized", message: "x", retryable: false } };
  expect(await fetchAllowedOrigins(API, null, respond(200, { allowed_origins: ["https://a.b"] }))).toEqual([]);
  expect(await fetchAllowedOrigins(API, "wk_bad", respond(401, unauthorized))).toEqual([]);
  expect(await fetchAllowedOrigins(API, "wk_1", respond(500, {}))).toEqual([]);
  expect(await fetchAllowedOrigins(API, "wk_1", respond(200, { origins: [] }))).toEqual([]);
  expect(await fetchAllowedOrigins(API, "wk_1", respond(200, null))).toEqual([]);
  const notJson = (async () => new Response("<html>")) as unknown as typeof fetch;
  expect(await fetchAllowedOrigins(API, "wk_1", notJson)).toEqual([]);
  const down = (async () => {
    throw new TypeError("fetch failed");
  }) as unknown as typeof fetch;
  expect(await fetchAllowedOrigins(API, "wk_1", down)).toEqual([]);
});
