import { type Page, expect, test } from "@playwright/test";
import { WEB_URL, WIDGET_KEY } from "./env";

// Виджет на сайте тенанта (ADR-0022): widget.js → iframe `/widget` с CSP frame-ancestors.
// Демо-страница открывается с WEB_URL — он в allowed_origins демо-тенантов (config/tenants).

// Чужой сайт: страница подменяется Playwright, origin отличается от разрешённых только портом.
const FOREIGN_SITE = "http://127.0.0.2:3102/";

test("CSP страницы iframe: allowed_origins ключа, иначе 'none'", async ({ request }) => {
  const csp = async (query: string) =>
    (await request.get(`/widget${query}`)).headers()["content-security-policy"];

  expect(await csp(`?key=${WIDGET_KEY}`)).toBe(`frame-ancestors http://localhost:3000 ${WEB_URL}`);
  expect(await csp("?key=wk_unknown")).toBe("frame-ancestors 'none'");
  expect(await csp("")).toBe("frame-ancestors 'none'");
  // Полноэкранный чат proxy не трогает.
  expect((await request.get(`/?key=${WIDGET_KEY}`)).headers()["content-security-policy"]).toBeUndefined();
});

async function openWidget(page: Page) {
  await page.goto(`/demo/widget?key=${WIDGET_KEY}`);
  await page.getByRole("button", { name: "Открыть чат" }).click();
  const chat = page.frameLocator('iframe[title="Чат с консультантом"]');
  await expect(chat.getByTestId("greeting")).toBeVisible();
  return chat;
}

test("демо-сайт: открыть, написать, свернуть, открыть снова — диалог на месте", async ({ page }) => {
  const chat = await openWidget(page);
  const panel = page.locator('[data-tessera="panel"]');
  const box = await panel.boundingBox();
  expect(box?.width).toBe(400); // десктоп — панель в углу, а не на весь экран

  await chat.getByLabel("Сообщение").fill("Нужен подарок маме");
  await chat.getByLabel("Сообщение").press("Enter");
  const assistant = chat.locator('li[data-role="assistant"]');
  await expect(assistant).toHaveAttribute("data-status", "completed");

  await chat.getByRole("button", { name: "Свернуть" }).click();
  await expect(panel).toBeHidden();
  const launcher = page.getByRole("button", { name: "Открыть чат" });
  await expect(launcher).toBeFocused();

  await launcher.click();
  await expect(panel).toBeVisible();
  await expect(assistant).toHaveText("Вы написали: Нужен подарок маме");
  // Кнопка на десктопе остаётся и тоже сворачивает.
  await page.getByRole("button", { name: "Закрыть чат" }).click();
  await expect(panel).toBeHidden();
});

test("мобильный: чат на весь экран, кнопка скрыта, «Свернуть» возвращает кнопку", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const chat = await openWidget(page);
  const panel = page.locator('[data-tessera="panel"]');

  expect(await panel.boundingBox()).toEqual({ x: 0, y: 0, width: 390, height: 844 });
  await expect(page.locator('[data-tessera="launcher"]')).toBeHidden();
  await expect(chat.getByLabel("Сообщение")).toBeInViewport();

  await chat.getByRole("button", { name: "Свернуть" }).click();
  await expect(panel).toBeHidden();
  await expect(page.getByRole("button", { name: "Открыть чат" })).toBeVisible();
});

test("чужой сайт: браузер блокирует iframe по frame-ancestors", async ({ page, context }) => {
  // Подменённую страницу Chrome считает публичной и без разрешения не пускает её к loopback.
  await context.grantPermissions(["local-network-access"], { origin: FOREIGN_SITE.slice(0, -1) });
  await page.route(FOREIGN_SITE, (route) =>
    route.fulfill({
      contentType: "text/html",
      body: `<!doctype html><html><head><meta charset="utf-8"></head><body><h1>Чужой сайт</h1>
        <script src="${WEB_URL}/widget.js" data-key="${WIDGET_KEY}"></script></body></html>`,
    }),
  );
  const violation = page.waitForEvent("console", (msg) => msg.text().includes("frame-ancestors"));

  await page.goto(FOREIGN_SITE);
  await page.getByRole("button", { name: "Открыть чат" }).click();

  expect((await violation).text()).toContain(`frame-ancestors http://localhost:3000 ${WEB_URL}`);
  await expect(page.frameLocator("iframe").getByLabel("Сообщение")).toHaveCount(0);
});
