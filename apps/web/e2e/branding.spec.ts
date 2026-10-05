import { type Page, expect, test } from "@playwright/test";
import { GARDEN_WIDGET_KEY, WIDGET_KEY } from "./env";

// Брендинг из public config (config/tenants/*.yaml): два демо-тенанта на одном ядре выглядят
// по-разному. Эталоны скриншотов — в branding.spec.ts-snapshots; обновить после намеренных
// изменений вёрстки или токенов: `pnpm exec playwright test branding --update-snapshots`.
const TENANTS = [
  {
    slug: "demo-beauty",
    key: WIDGET_KEY,
    name: "Консультант Lumière",
    logo: "/demo/lumiere.svg",
    greeting: /Помогу подобрать уход за лицом/,
    starter: "Найти аромат",
    primary: "rgb(140, 90, 107)", // #8C5A6B
    radius: "12px",
    font: /^"?Inter"?,/,
  },
  {
    slug: "demo-garden",
    key: GARDEN_WIDGET_KEY,
    name: "Садовник «Зелёного угла»",
    logo: "/demo/garden.svg",
    greeting: /Подскажу, что посадить/,
    starter: "Что посадить в тени",
    primary: "rgb(46, 107, 63)", // #2E6B3F
    radius: "2px",
    font: /^"?Georgia"?,/,
  },
] as const;

async function openChat(page: Page, key: string) {
  await page.goto(`/?key=${key}`);
  // Конфиг грузится на клиенте: до ответа — нейтральная тема и «AI-консультант».
  await expect(page.getByTestId("greeting")).toBeVisible();
  await page.waitForFunction(() => [...document.images].every((img) => img.complete));
}

for (const tenant of TENANTS) {
  test(`${tenant.slug}: имя, логотип, приветствие и токены из конфига тенанта`, async ({ page }) => {
    await openChat(page, tenant.key);

    await expect(page.getByRole("heading", { level: 1 })).toHaveText(tenant.name);
    await expect(page.locator("header img")).toHaveAttribute("src", tenant.logo);
    await expect(page.getByTestId("greeting")).toHaveText(tenant.greeting);
    const quick = page.getByRole("group", { name: "Быстрые ответы" });
    await expect(quick.getByRole("button", { name: tenant.starter })).toBeVisible();

    await page.getByLabel("Сообщение").fill("привет");
    const send = page.getByRole("button", { name: "Отправить" });
    await expect(send).toHaveCSS("background-color", tenant.primary);
    await expect(send).toHaveCSS("border-radius", tenant.radius);
    const font = await page.locator("main").evaluate((el) => getComputedStyle(el).fontFamily);
    expect(font).toMatch(tenant.font);
  });

  test(`${tenant.slug}: скриншот пустого чата`, async ({ page }) => {
    await page.setViewportSize({ width: 800, height: 600 });
    await openChat(page, tenant.key);
    await expect(page).toHaveScreenshot(`${tenant.slug}.png`);
  });
}

test("приветствие только на пустом чате и не попадает в историю", async ({ page }) => {
  await openChat(page, GARDEN_WIDGET_KEY);
  await page.getByLabel("Сообщение").fill("Что посадить?");
  await page.getByLabel("Сообщение").press("Enter");

  await expect(page.locator('li[data-role="assistant"]')).toHaveAttribute("data-status", "completed");
  await expect(page.getByTestId("greeting")).toHaveCount(0);
  await page.reload();
  await expect(page.locator('li[data-role="user"]')).toHaveText("Что посадить?");
  await expect(page.getByTestId("greeting")).toHaveCount(0);
  await expect(page.locator("li")).toHaveCount(2);
});
