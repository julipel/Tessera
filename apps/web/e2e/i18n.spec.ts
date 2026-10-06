import { expect, test } from "@playwright/test";
import { GARDEN_WIDGET_KEY } from "./env";

// demo-garden — language: auto с переводом en (config/tenants/demo-garden.yaml): язык диалога
// выбирается по языку браузера (ADR-0025). Русский интерфейс того же тенанта — branding.spec.ts.
test.use({ locale: "en-US" });

test("язык браузера en: интерфейс, приветствие и подсказки на английском, locale в API", async ({
  page,
}) => {
  const config = page.waitForRequest((r) => r.url().includes("/v1/public/config"));
  await page.goto(`/?key=${GARDEN_WIDGET_KEY}`);

  expect(new URL((await config).url()).searchParams.get("locale")).toBe("en-US");
  await expect(page.getByTestId("greeting")).toHaveText(/I can help you choose what to plant/);
  const quick = page.getByRole("group", { name: "Quick replies" });
  await expect(quick.getByRole("button", { name: "What to plant in the shade" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");

  const created = page.waitForRequest((r) => r.url().endsWith("/v1/conversations") && r.method() === "POST");
  await page.getByLabel("Message", { exact: true }).fill("Hello");
  await page.getByRole("button", { name: "Send" }).click();

  expect((await created).postDataJSON()).toMatchObject({ locale: "en-US" });
  await expect(page.locator('li[data-role="assistant"]')).toHaveAttribute("data-status", "completed");
  await expect(page.getByRole("list", { name: "Messages" })).toBeVisible();
});
