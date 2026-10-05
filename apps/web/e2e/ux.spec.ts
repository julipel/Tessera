import { type Locator, type Page, expect, test } from "@playwright/test";
import { WIDGET_KEY } from "./env";

// Мок модели (e2e/mock-llm.mjs): на «долго» — 40 абзацев «Строка N.» по 100 мс,
// на «сбой» — первый запрос с этим текстом падает (llm_unavailable, retryable).

async function say(page: Page, text: string): Promise<void> {
  await page.getByLabel("Сообщение").fill(text);
  await page.getByLabel("Сообщение").press("Enter");
}

/** Расстояние от низа ленты сообщений до низа видимой области. */
function gapToBottom(scroller: Locator): Promise<number> {
  return scroller.evaluate((el) => el.scrollHeight - el.scrollTop - el.clientHeight);
}

test("«Остановить» прерывает ответ: частичный текст сохраняется и после перезагрузки", async ({
  page,
}) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  await say(page, "Расскажи долго");

  const assistant = page.locator('li[data-role="assistant"]');
  await expect(assistant).toContainText("Строка 2.");
  await page.getByRole("button", { name: "Остановить" }).click();

  await expect(assistant).toHaveAttribute("data-status", "interrupted");
  await expect(assistant).toContainText("Ответ прерван");
  await expect(assistant).not.toContainText("Строка 40.");
  await expect(page.getByRole("button", { name: "Отправить" })).toBeVisible();
  await expect(page.getByRole("status")).toHaveCount(0);

  await page.reload();
  await expect(assistant).toHaveAttribute("data-status", "interrupted");
  await expect(assistant).toContainText("Строка 2.");
});

test("перезагрузка во время ответа: диалог восстанавливается с прерванным ответом", async ({
  page,
}) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  await say(page, "Ответь долго");
  const assistant = page.locator('li[data-role="assistant"]');
  await expect(assistant).toContainText("Строка 2.");

  await page.reload();
  await expect(page.locator('li[data-role="user"]')).toHaveText("Ответь долго");
  await expect(assistant).toHaveAttribute("data-status", "interrupted");
  await expect(assistant).toContainText("Строка 2.");
  // Восстановленный диалог — не пустой чат: приветствия и стартовых подсказок нет.
  await expect(page.getByTestId("greeting")).toHaveCount(0);
  await expect(page.getByRole("group", { name: "Быстрые ответы" })).toHaveCount(0);
});

test("ошибка модели: «Повторить» отправляет тот же ввод и заменяет неудачную попытку", async ({
  page,
}) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  await say(page, "Проверка сбой номер один");

  const assistant = page.locator('li[data-role="assistant"]');
  await expect(assistant).toHaveAttribute("data-status", "failed");
  await expect(assistant).toContainText("модель сейчас недоступна");
  await page.getByRole("button", { name: "Повторить" }).click();

  await expect(assistant).toHaveText("Вы написали: Проверка сбой номер один");
  await expect(assistant).toHaveAttribute("data-status", "completed");
  await expect(page.locator('li[data-role="user"]')).toHaveCount(1);
  await expect(page.getByRole("button", { name: "Повторить" })).toHaveCount(0);
});

test("автоскролл: лента едет за ответом, прокрутка вверх останавливает её до кнопки", async ({
  page,
}) => {
  await page.setViewportSize({ width: 400, height: 600 });
  await page.goto(`/?key=${WIDGET_KEY}`);
  await say(page, "Напиши долго");

  const assistant = page.locator('li[data-role="assistant"]');
  const scroller = page.getByRole("list", { name: "Сообщения" }).locator("..");
  const toLatest = page.getByRole("button", { name: "К новым сообщениям" });

  // Ответ заметно длиннее ленты: прокрутка вверх точно уводит от низа.
  await expect(assistant).toContainText("Строка 16.");
  expect(await gapToBottom(scroller)).toBeLessThanOrEqual(80);
  await expect(toLatest).toHaveCount(0);

  // Посетитель прокрутил вверх — новые строки не утаскивают ленту вниз.
  await scroller.evaluate((el) => el.scrollTo({ top: 0 }));
  await expect(toLatest).toBeVisible();
  await expect(assistant).toContainText("Строка 22.");
  expect(await scroller.evaluate((el) => el.scrollTop)).toBe(0);

  await toLatest.click();
  await expect(toLatest).toHaveCount(0);
  await expect(assistant).toHaveAttribute("data-status", "completed", { timeout: 10_000 });
  expect(await gapToBottom(scroller)).toBeLessThanOrEqual(80);
});
