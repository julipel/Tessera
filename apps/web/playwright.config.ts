import { defineConfig, devices } from "@playwright/test";
import {
  API_PORT,
  API_URL,
  E2E_ENV,
  HOST,
  MOCK_LLM_PORT,
  MOCK_LLM_URL,
  WEB_PORT,
  WEB_URL,
} from "./e2e/env";

// Проверка готовности webServer идёт через HTTP_PROXY, если он задан, а маски вида `127.*`
// в NO_PROXY Node не понимает — исключаем хост e2e явно.
for (const name of ["NO_PROXY", "no_proxy"]) {
  process.env[name] = [process.env[name], HOST].filter(Boolean).join(",");
}

// E2E: настоящий API на отдельной базе и порту (модель — мок OpenAI-совместимого API)
// + Next dev — не мешают dev-серверам на :8000/:3000. Нужен `make up` (Postgres). Юнит-тесты `*.unit.spec.ts` идут без браузера.
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: WEB_URL,
    // Язык браузера — как у посетителей демо-тенантов: demo-garden (language: auto) выбирает
    // язык диалога по нему (ADR-0025). Английский интерфейс — i18n.spec.ts.
    locale: "ru-RU",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "node e2e/mock-llm.mjs",
      url: `${MOCK_LLM_URL}/health`,
      env: { MOCK_LLM_HOST: HOST, MOCK_LLM_PORT: String(MOCK_LLM_PORT) },
      reuseExistingServer: false,
    },
    {
      command: `uv run uvicorn app.main:app --host ${HOST} --port ${API_PORT}`,
      cwd: "../api",
      url: `${API_URL}/health`,
      env: E2E_ENV,
      reuseExistingServer: false,
    },
    {
      command: `pnpm exec next dev --hostname ${HOST} --port ${WEB_PORT}`,
      url: WEB_URL,
      env: { NEXT_PUBLIC_API_URL: API_URL },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
