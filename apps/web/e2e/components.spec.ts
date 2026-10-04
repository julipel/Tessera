import { expect, test } from "@playwright/test";
import { DEMO_SECTIONS } from "../src/components/rich/fixtures";

// Скриншоты витрины UI-компонентов (/demo/components). Эталоны — в components.spec.ts-snapshots;
// обновить после намеренных изменений вёрстки: `pnpm exec playwright test components --update-snapshots`.
const SECTIONS = ["message", ...DEMO_SECTIONS.map((s) => s.id)];

// Нативные контролы (дата) зависят от локали браузера.
test.use({ locale: "ru-RU" });

for (const [device, viewport] of [
  ["desktop", { width: 1280, height: 900 }],
  ["mobile", { width: 390, height: 844 }],
] as const) {
  test.describe(device, () => {
    test.use({ viewport });

    test("все компоненты рендерятся", async ({ page }) => {
      await page.goto("/demo/components");
      // Картинки локальные — дожидаемся загрузки, чтобы скриншоты были стабильными.
      await page.waitForFunction(() => [...document.images].every((img) => img.complete));
      for (const id of SECTIONS) {
        await expect(page.getByTestId(id)).toHaveScreenshot(`${id}-${device}.png`);
      }
    });
  });
}

test("семантика: ссылки, таблица, действия без обработчика отключены, markdown безопасен", async ({ page }) => {
  await page.goto("/demo/components");

  const card = page.getByTestId("product-card").getByRole("article", { name: /Увлажняющий крем/ });
  await expect(card.getByRole("link", { name: /Увлажняющий крем/ })).toHaveAttribute("href", "https://example.com/cream");
  await expect(card.getByText("1 990 ₽")).toBeVisible();
  await expect(card.getByRole("button", { name: "Выбрать" })).toBeDisabled(); // отправка action — P5-03
  await expect(card.getByRole("link", { name: "На сайте" })).toHaveAttribute("target", "_blank");

  const table = page.getByTestId("comparison-table").getByRole("table");
  await expect(table.getByRole("columnheader")).toHaveCount(3);
  await expect(table.getByRole("row", { name: /Наличие/ }).getByRole("cell").last()).toHaveText("—");

  await expect(page.getByTestId("form").getByLabel("Телефон")).toHaveAttribute("type", "tel");
  await expect(page.getByTestId("form").getByRole("button", { name: "Отправить заявку" })).toBeDisabled();

  const message = page.getByTestId("message");
  await expect(message.locator("strong")).toHaveText("сухой кожи");
  await expect(message.locator("ol > li")).toHaveCount(2);
  await expect(message.locator("[data-block=text] b, [data-block=text] img")).toHaveCount(0);
});
