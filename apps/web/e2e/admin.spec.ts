import { expect, type Page, test } from "@playwright/test";
import { ADMIN_EMAIL, ADMIN_PASSWORD } from "./env";

// Админка: вход, версии конфига и источники тенанта e2e-admin (e2e/fixtures/admin-tenant.yaml).
// Тесты меняют активную версию одного тенанта — по очереди.
test.describe.configure({ mode: "serial" });

async function login(page: Page, password = ADMIN_PASSWORD) {
  await page.goto("/admin/login");
  await page.getByLabel("Email").fill(ADMIN_EMAIL);
  await page.getByLabel("Пароль").fill(password);
  await page.getByRole("button", { name: "Войти" }).click();
}

test("без входа — страница входа; неверный пароль — ошибка без уточнения причины", async ({ page }) => {
  await page.goto("/admin");
  await expect(page).toHaveURL(/\/admin\/login$/);

  await login(page, "wrong password!!");
  await expect(page.getByRole("main").getByRole("alert")).toHaveText("Неверный email или пароль");
  await expect(page).toHaveURL(/\/admin\/login$/);
});

test("конфиг: ошибка проверки, черновик, активация, откат; выход", async ({ page }) => {
  page.on("dialog", (dialog) => void dialog.accept());
  await login(page);
  await expect(page.getByText(ADMIN_EMAIL)).toBeVisible();
  await page.getByRole("link", { name: "Конфиг агента" }).click();

  const versions = page.getByRole("navigation", { name: "Версии конфига" }).getByRole("button");
  const editor = page.getByLabel("YAML конфига");
  const save = page.getByRole("button", { name: "Сохранить черновик" });
  // Открыта активная версия; без правок сохранять нечего.
  const active = versions.and(page.locator('[data-status="active"]'));
  await expect(active).toHaveCount(1);
  const activeLabel = (await active.locator("span").first().textContent()) ?? "";
  await expect(editor).toHaveValue(/greeting: Здравствуйте!/);
  await expect(save).toBeDisabled();

  // Конфиг, не прошедший схему, — ошибка с местом в конфиге, версия не создаётся.
  const original = await editor.inputValue();
  const count = await versions.count();
  await editor.fill(original.replace("limits: {}", "limits:\n  max_steps: 0"));
  await save.click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("limits.max_steps");
  await expect(versions).toHaveCount(count);

  // Верный конфиг — новый черновик сверху; активная версия не меняется.
  await editor.fill(original.replace("Здравствуйте!", "Добрый день!"));
  await save.click();
  await expect(page.getByRole("status")).toContainText("Сохранено: черновик");
  await expect(versions).toHaveCount(count + 1);
  await expect(versions.first()).toHaveAttribute("data-status", "draft");
  await expect(versions.first()).toHaveAttribute("aria-current", "true");
  await expect(active.locator("span").first()).toHaveText(activeLabel);

  await page.getByRole("button", { name: "Активировать" }).click();
  await expect(versions.first()).toHaveAttribute("data-status", "active");
  await expect(page.getByRole("status")).toContainText("Активна");

  // Откат — прежняя активная версия, теперь архивная.
  const previous = versions.filter({ hasText: new RegExp(`^${activeLabel}\\b`) });
  await previous.click();
  await expect(editor).toHaveValue(/greeting: Здравствуйте!/);
  await page.getByRole("button", { name: "Откатить на эту версию" }).click();
  await expect(previous).toHaveAttribute("data-status", "active");
  await expect(versions.first()).toHaveAttribute("data-status", "archived");

  await page.getByRole("button", { name: "Выйти" }).click();
  await expect(page).toHaveURL(/\/admin\/login$/);
  await page.goto("/admin");
  await expect(page).toHaveURL(/\/admin\/login$/);
});

test("источники: ошибка конфига, файловый источник, загрузка и удаление файла, синхронизация", async ({ page }) => {
  page.on("dialog", (dialog) => void dialog.accept());
  // База e2e между прогонами не очищается — имя источника уникально на прогон.
  const name = `kb-${Date.now()}`;
  await login(page);
  await page.getByRole("link", { name: "Источники" }).click();
  await expect(page.getByRole("heading", { name: /^Источники/ })).toBeVisible();

  // Конфиг сайта без start_urls — ошибка с местом в конфиге, источник не создаётся.
  await page.getByLabel("Имя").fill(name);
  await page.getByLabel("Конфиг (YAML)").fill("max_pages: 10");
  await page.getByRole("button", { name: "Добавить" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("config_yaml.start_urls");

  await page.getByLabel("Вид").selectOption("file");
  await page.getByLabel("Конфиг (YAML)").fill("");
  await page.getByRole("button", { name: "Добавить" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByText("Файлов нет.")).toBeVisible();

  const upload = page.getByLabel(/Загрузить файлы/);
  await upload.setInputFiles({ name: "faq.md", mimeType: "text/markdown", buffer: Buffer.from("# FAQ\n\nОтвет.") });
  await expect(page.getByRole("status")).toContainText("Загружено файлов: 1");
  const files = page.getByRole("list", { name: "Файлы источника" });
  await expect(files).toContainText("faq.md");

  await upload.setInputFiles({ name: "run.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ") });
  await expect(page.getByRole("main").getByRole("alert")).toContainText("run.exe");
  await expect(files.getByRole("listitem")).toHaveCount(1);

  await page.getByRole("button", { name: "Удалить faq.md" }).click();
  await expect(page.getByRole("status")).toContainText("после полной синхронизации");
  await expect(page.getByText("Файлов нет.")).toBeVisible();

  // Воркера в e2e нет — синхронизация остаётся в очереди (выполнение проверяет pytest).
  await page.getByRole("button", { name: "Синхронизировать" }).click();
  const history = page.getByRole("table", { name: "История синхронизаций" });
  await expect(history.getByRole("row").nth(1)).toContainText("в очереди");
  await expect(page.getByRole("button", { name: "Синхронизировать" })).toBeDisabled();

  await page.getByRole("link", { name: "← Источники" }).click();
  await expect(page.getByRole("row", { name: new RegExp(name) })).toContainText("в очереди");
});
