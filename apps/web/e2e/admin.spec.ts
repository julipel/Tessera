import { expect, type Page, test } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { ADMIN_EMAIL, ADMIN_PASSWORD, ADMIN_WIDGET_KEY, API_URL } from "./env";

// Админка: вход, версии конфига, источники и диалоги тенанта e2e-admin (e2e/fixtures/admin-tenant.yaml).
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

test("сессия — HttpOnly-cookie API, скрипту страницы токен недоступен (ADR-0044)", async ({ page, context }) => {
  await login(page);
  await expect(page.getByText(ADMIN_EMAIL)).toBeVisible();

  // Без URL: фильтр по http-адресу отбрасывает Secure-cookie, хотя браузер шлёт её на localhost.
  const [cookie] = (await context.cookies()).filter((c) => c.name === "tessera_admin");
  expect(cookie).toMatchObject({ httpOnly: true, secure: true, sameSite: "Strict", path: "/v1/admin" });
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + document.cookie);
  expect(stored).not.toContain(cookie.value);

  // Перезагрузка: сессия жива без всякого клиентского хранилища.
  await page.reload();
  await expect(page.getByText(ADMIN_EMAIL)).toBeVisible();
});

test("CSP админки: nonce на запрос, frame-ancestors 'none', внедрённый обработчик не выполняется", async ({
  page,
}) => {
  const first = await page.goto("/admin/login");
  const csp = (await first?.headerValue("content-security-policy")) ?? "";
  expect(csp).toMatch(/script-src 'self' 'nonce-[A-Za-z0-9+/=]+' 'strict-dynamic'/);
  expect(csp).toContain("frame-ancestors 'none'");
  expect(csp).toMatch(/script-src [^;]*'strict-dynamic'/);
  expect(/script-src [^;]*/.exec(csp)?.[0]).not.toContain("unsafe-inline");
  const nonce = /'nonce-([^']+)'/.exec(csp)?.[1];
  // Next расставил nonce своим скриптам — страница живая (форма входа работает ниже).
  expect(await page.locator(`script[nonce]`).count()).toBeGreaterThan(0);

  const again = await page.goto("/admin/login");
  expect(/'nonce-([^']+)'/.exec((await again?.headerValue("content-security-policy")) ?? "")?.[1]).not.toBe(
    nonce,
  );

  // Разметка с inline-обработчиком (типичная XSS через innerHTML): CSP без 'unsafe-inline'
  // его не выполняет и сообщает о нарушении.
  const outcome = await page.evaluate(
    () =>
      new Promise<{ ran: boolean; violated: string | null }>((resolve) => {
        const w = window as unknown as { __xss?: boolean };
        let violated: string | null = null;
        document.addEventListener("securitypolicyviolation", (e) => {
          violated = e.effectiveDirective;
        });
        const host = document.createElement("div");
        host.innerHTML = '<img src="data:," onerror="window.__xss = true">';
        document.body.append(host);
        setTimeout(() => resolve({ ran: w.__xss === true, violated }), 500);
      }),
  );
  expect(outcome).toEqual({ ran: false, violated: "script-src-attr" });

  await login(page);
  await expect(page.getByText(ADMIN_EMAIL)).toBeVisible();
});

test("CSP админки: обычная работа без нарушений политики", async ({ page }) => {
  // Ловит сторонние скрипты и inline-обработчики в самой админке. e2e идут на next dev, где
  // style-src ослаблен под его оверлей: inline-стили проверяет только прод-сборка.
  const violations: string[] = [];
  page.on("console", (msg) => {
    if (/Content Security Policy/i.test(msg.text())) violations.push(msg.text());
  });
  await login(page);
  await expect(page.getByText(ADMIN_EMAIL)).toBeVisible();
  for (const name of ["Конфиг агента", "Источники", "Диалоги", "Заявки"]) {
    await page.goto("/admin");
    await page.getByRole("link", { name }).first().click();
    await expect(page.getByRole("main")).not.toContainText("Загрузка…");
  }

  expect(violations).toEqual([]);
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

  // Удаление само ставит полную синхронизацию (REVIEW.md: K-14). Воркера в e2e нет —
  // она остаётся в очереди (выполнение проверяет pytest).
  await page.getByRole("button", { name: "Удалить faq.md" }).click();
  await expect(page.getByRole("status")).toContainText("Синхронизация поставлена в очередь");
  await expect(page.getByText("Файлов нет.")).toBeVisible();
  const history = page.getByRole("table", { name: "История синхронизаций" });
  await expect(history.getByRole("row").nth(1)).toContainText("в очереди");
  await expect(page.getByRole("button", { name: "Синхронизировать" })).toBeDisabled();

  await page.getByRole("link", { name: "← Источники" }).click();
  await expect(page.getByRole("row", { name: new RegExp(name) })).toContainText("в очереди");
});

test("диалоги: список, ход по шагам, оценка ответа", async ({ page, request }) => {
  // Диалог с ходом, где мок вызывает suggest_replies (e2e/mock-llm.mjs, слово «варианты»).
  const headers = { "X-Widget-Key": ADMIN_WIDGET_KEY };
  const created = await request.post(`${API_URL}/v1/conversations`, {
    headers,
    data: { visitor_id: `e2e-${Date.now()}` },
  });
  expect(created.ok()).toBe(true);
  const { conversation_id } = (await created.json()) as { conversation_id: string };
  const question = `покажи варианты ${Date.now()}`;
  const turn = await request.post(`${API_URL}/v1/conversations/${conversation_id}/messages`, {
    headers,
    data: { client_message_id: randomUUID(), input: { type: "text", text: question } },
  });
  expect(await turn.text()).toContain("event: done");

  // Журнал страницами по 2 события: админка догружает все страницы (REVIEW.md: O-8).
  const journalPages: string[] = [];
  await page.route(/\/v1\/admin\/tenants\/[^/]+\/events\?/, async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("limit", "2");
    journalPages.push(url.searchParams.get("after") ?? "");
    await route.continue({ url: url.toString() });
  });

  await login(page);
  await page.getByRole("link", { name: "Диалоги" }).click();
  await expect(page.getByRole("heading", { name: /^Диалоги/ })).toBeVisible();
  const row = page.getByRole("row", { name: new RegExp(question) });
  await expect(row).toContainText("2");
  await row.getByRole("link", { name: question }).click();

  const answer = page.getByRole("article");
  await expect(answer).toContainText(`Вы написали: ${question}`);
  await answer.getByText(/^Ход по шагам/).click();
  const steps = answer.getByRole("list", { name: "Шаги хода" });
  await expect(steps).toContainText("начало хода");
  await expect(steps).toContainText("suggest_replies");
  await expect(steps).toContainText("быстрые ответы");
  await expect(steps.getByRole("listitem").last()).toContainText("итог completed");
  // Были запросы с курсором — журнал пришёл несколькими страницами и показан целиком.
  expect(journalPages.filter(Boolean).length).toBeGreaterThan(0);
  await expect(page.getByText("Журнал показан не полностью")).toHaveCount(0);

  // Плохая оценка с комментарием → диалог в фильтре «только с плохой оценкой».
  await answer.getByLabel("Комментарий к оценке").fill("не ответил на вопрос");
  await answer.getByRole("button", { name: "👎 Плохо" }).click();
  await expect(answer.getByRole("status")).toHaveText("Оценка сохранена");
  await expect(answer.getByRole("button", { name: "👎 Плохо" })).toHaveAttribute("aria-pressed", "true");
  await expect(answer).toContainText(ADMIN_EMAIL);

  await page.reload();
  await expect(page.getByRole("article").getByLabel("Комментарий к оценке")).toHaveValue("не ответил на вопрос");
  await page.getByRole("link", { name: "← Диалоги" }).click();
  await page.getByLabel("Только с плохой оценкой").check();
  await expect(page.getByRole("row", { name: new RegExp(question) })).toContainText("👎 1");

  // Снятая оценка — диалога в фильтре больше нет.
  await page.getByRole("row", { name: new RegExp(question) }).getByRole("link").click();
  await page.getByRole("article").getByRole("button", { name: "Снять оценку" }).click();
  await expect(page.getByRole("article").getByRole("status")).toHaveText("Оценка снята");
  await page.getByRole("link", { name: "← Диалоги" }).click();
  await page.getByLabel("Только с плохой оценкой").check();
  await expect(page.getByRole("row", { name: new RegExp(question) })).toHaveCount(0);
});

test("заявки: список, поиск по номеру, фильтр по форме, переход в диалог, CSV", async ({ page }) => {
  // Заявку создаёт настоящий ход: форма в виджете тенанта e2e-admin → подтверждение
  // (мок модели: «консультация» → show_form, отправленная форма → create_lead).
  const visitor = `Анна ${Date.now()}`;
  await page.goto(`/?key=${ADMIN_WIDGET_KEY}`);
  await page.getByLabel("Сообщение").fill("Хочу консультацию");
  await page.getByLabel("Сообщение").press("Enter");
  const form = page.getByRole("form", { name: "Консультация E2E" });
  await form.getByLabel("Имя").fill(visitor);
  await form.getByLabel("Телефон или Telegram").fill("=HYPERLINK(1)");
  await form.getByRole("button", { name: "Отправить" }).click();
  const confirm = page.getByRole("group", { name: "Подтверждение" });
  await confirm.getByRole("button", { name: "Подтвердить" }).click();
  await expect(page.locator('li[data-role="assistant"]').last()).toContainText("Заявка создана");

  await login(page);
  await page.getByRole("link", { name: "Заявки" }).click();
  await expect(page.getByRole("heading", { name: /^Заявки/ })).toBeVisible();
  const row = page.getByRole("row", { name: new RegExp(visitor) });
  await expect(row).toContainText("Консультация E2E");
  await expect(row).toContainText("Телефон или Telegram: =HYPERLINK(1)");
  const number = /^№ (\d+)$/.exec((await row.getByRole("cell").first().textContent()) ?? "")?.[1];
  expect(number).toBeTruthy();

  // Поиск по номеру — одна заявка; несуществующий номер — «не найдено»; сброс — снова все.
  const search = page.getByRole("search", { name: "Поиск по номеру" });
  await search.getByLabel("Номер заявки").fill(number!);
  await search.getByRole("button", { name: "Найти" }).click();
  await expect(page.getByRole("table", { name: "Заявки" }).getByRole("row")).toHaveCount(2);
  await expect(row).toBeVisible();
  await search.getByLabel("Номер заявки").fill("2147483647");
  await search.getByRole("button", { name: "Найти" }).click();
  await expect(page.getByText("Заявок не найдено.")).toBeVisible();
  await search.getByRole("button", { name: "Сбросить" }).click();
  await expect(row).toBeVisible();

  // Фильтр по форме — форма из загруженных заявок, запрос идёт с её ключом.
  const filtered = page.waitForRequest((r) => new URL(r.url()).searchParams.get("form_key") === "consultation");
  await page.getByLabel("Форма").selectOption({ label: "Консультация E2E" });
  await filtered;
  await expect(row).toBeVisible();

  // CSV: имя файла, BOM для Excel, строка заявки с защитой от формулы.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Скачать CSV" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("leads.csv");
  const csv = await readFile((await download.path())!, "utf8");
  expect(csv.startsWith("\uFEFF")).toBe(true);
  const line = csv.split("\r\n").find((l) => l.includes(visitor));
  expect(line?.startsWith(`${number};`)).toBe(true);
  expect(line).toContain("'=HYPERLINK(1)");

  // Ошибка выгрузки (422 — больше 10 000 заявок) — на странице, файл не скачивается.
  await page.route(/\/leads\.csv/, (route) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "invalid_input", message: "Сузьте период" } }),
    }),
  );
  let downloaded = false;
  page.on("download", () => (downloaded = true));
  await page.getByRole("button", { name: "Скачать CSV" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("Сузьте период");
  expect(downloaded).toBe(false);
  await page.unroute(/\/leads\.csv/);

  // «С» позже «по» — ошибка до запроса.
  await page.getByLabel("С", { exact: true }).fill("2026-10-02");
  await page.getByLabel("По", { exact: true }).fill("2026-10-01");
  await page.getByRole("button", { name: "Скачать CSV" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toHaveText("Дата «с» позже даты «по»");

  // Переход в диалог заявки.
  await row.getByRole("link", { name: "Открыть" }).click();
  await expect(page.getByRole("heading", { name: /^Диалог/ })).toBeVisible();
  await expect(page.getByText("Хочу консультацию").first()).toBeVisible();
});
