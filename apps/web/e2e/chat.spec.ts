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
  // Пока ход идёт, отправка заблокирована; эхо-агент отдаёт текст по словам.
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
