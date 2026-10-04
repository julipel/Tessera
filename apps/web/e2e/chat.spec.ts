import { expect, test } from "@playwright/test";
import { WIDGET_KEY } from "./env";

test("отправка сообщения: ответ стримится, после перезагрузки диалог восстанавливается", async ({
  page,
}) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  const input = page.getByLabel("Сообщение");
  const send = page.getByRole("button", { name: "Отправить" });

  await input.fill("Нужен подарок маме");
  await input.press("Enter");

  const user = page.locator('li[data-role="user"]');
  const assistant = page.locator('li[data-role="assistant"]');
  await expect(user).toHaveText("Нужен подарок маме");
  // Пока ход идёт, отправка заблокирована; мок модели отдаёт текст по словам.
  await expect(send).toBeDisabled();
  await expect(assistant).toHaveText("Вы написали: Нужен подарок маме");
  await expect(assistant).toHaveAttribute("data-status", "completed");
  await expect(page.getByRole("status")).toHaveCount(0);

  await page.reload();
  await expect(user).toHaveText("Нужен подарок маме");
  await expect(assistant).toHaveText("Вы написали: Нужен подарок маме");
});

test("неверный ключ виджета — ошибка вместо ответа", async ({ page }) => {
  await page.goto("/?key=wk_unknown");
  await page.getByLabel("Сообщение").fill("привет");
  await page.getByRole("button", { name: "Отправить" }).click();

  await expect(page.getByText("неизвестный ключ виджета")).toBeVisible();
  await expect(page.getByRole("button", { name: "Отправить" })).toBeDisabled(); // пустой ввод
  await expect(page.getByRole("status")).toHaveCount(0);
});

test("стартовые подсказки на пустом чате отправляются как текст", async ({ page }) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  const quick = page.getByRole("group", { name: "Быстрые ответы" });
  // starter_suggestions демо-тенанта (config/tenants/demo-beauty.yaml).
  await quick.getByRole("button", { name: "Найти аромат" }).click();

  await expect(page.locator('li[data-role="user"]')).toHaveText("Найти аромат");
  await expect(page.locator('li[data-role="assistant"]')).toHaveText("Вы написали: Найти аромат");
  await expect(quick).toHaveCount(0);
});

test("быстрые ответы от suggest_replies: под последним ответом, нажатие отправляет текст", async ({
  page,
}) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  // Мок модели на слово «варианты» вызывает suggest_replies(["Сухая", "Жирная"]).
  await page.getByLabel("Сообщение").fill("Покажи варианты");
  await page.getByLabel("Сообщение").press("Enter");

  const quick = page.getByRole("group", { name: "Быстрые ответы" });
  await expect(quick.getByRole("button")).toHaveText(["Сухая", "Жирная"]);
  await expect(page.locator('li[data-role="assistant"]')).toHaveAttribute("data-status", "completed");

  await quick.getByRole("button", { name: "Сухая" }).click();
  await expect(page.locator('li[data-role="user"]').last()).toHaveText("Сухая");
  await expect(page.locator('li[data-role="assistant"]').last()).toHaveText("Вы написали: Сухая");
  // У нового ответа подсказок нет — старые не показываются.
  await expect(quick).toHaveCount(0);
});
