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

test("форма и подтверждение: show_form → отправка формы → confirm → заявка создана", async ({
  page,
}) => {
  await page.goto(`/?key=${WIDGET_KEY}`);
  // Мок модели на «консультацию» вызывает show_form(consultation) из конфига демо-тенанта,
  // на отправленную форму — create_lead с её значениями.
  await page.getByLabel("Сообщение").fill("Хочу консультацию косметолога");
  await page.getByLabel("Сообщение").press("Enter");

  const user = page.locator('li[data-role="user"]');
  const assistant = page.locator('li[data-role="assistant"]');
  const form = page.getByRole("form", { name: "Консультация косметолога" });
  const submit = form.getByRole("button", { name: "Отправить" });
  await expect(assistant.last()).toHaveAttribute("data-status", "completed");
  await expect(submit).toBeEnabled();

  // Обязательные поля не заполнены — браузер не даёт отправить.
  await submit.click();
  await expect(user).toHaveCount(1);

  await form.getByLabel("Имя").fill("Анна");
  await form.getByLabel("Телефон или Telegram").fill("@anna");
  await submit.click();

  // В истории — заголовок формы, а не значения; пока идёт ход, форма не отправляется.
  await expect(user.last()).toHaveText("Консультация косметолога");
  await expect(submit).toBeDisabled();

  const confirm = page.getByRole("group", { name: "Подтверждение" });
  await expect(confirm).toContainText("Имя: Анна; Телефон или Telegram: @anna");
  await expect(assistant.last()).toHaveAttribute("data-status", "completed");

  await confirm.getByRole("button", { name: "Подтвердить" }).click();

  await expect(user.last()).toHaveText("Подтвердить");
  // Заявку создаёт бэкенд по нажатию, модель получает итог вместе с нажатием.
  await expect(assistant.last()).toContainText("Подтверждено, create_lead: Заявка создана");
  await expect(assistant.last()).toHaveAttribute("data-status", "completed");
});
