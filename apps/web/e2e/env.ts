// Окружение e2e: своя база `app_e2e` (pytest использует `app_test` с откатом транзакций).
// 127.0.0.2, а не localhost: в WSL с networkingMode=mirrored подключение к ещё не открытому
// порту 127.0.0.1 висит ~2 мин вместо отказа, и проверка webServer Playwright зависает.
export const HOST = "127.0.0.2";
export const API_PORT = 8001;
export const WEB_PORT = 3001;
export const API_URL = `http://${HOST}:${API_PORT}`;
export const WEB_URL = `http://${HOST}:${WEB_PORT}`;
export const WIDGET_KEY = "wk_e2e_demo_beauty";

export const E2E_ENV: Record<string, string> = {
  APP_ENV: "test",
  DATABASE_URL:
    process.env.E2E_DATABASE_URL ?? "postgresql+asyncpg://app:app@localhost:5432/app_e2e",
  CORS_ORIGINS: JSON.stringify([WEB_URL]),
};
