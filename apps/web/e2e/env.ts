// Окружение e2e: своя база `app_e2e` (pytest использует `app_test` с откатом транзакций).
// 127.0.0.2, а не localhost: в WSL с networkingMode=mirrored подключение к ещё не открытому
// порту 127.0.0.1 висит ~2 мин вместо отказа, и проверка webServer Playwright зависает.
export const HOST = "127.0.0.2";
export const API_PORT = 8001;
export const WEB_PORT = 3001;
export const API_URL = `http://${HOST}:${API_PORT}`;
export const WEB_URL = `http://${HOST}:${WEB_PORT}`;
export const WIDGET_KEY = "wk_e2e_demo_beauty";
// Второй демо-тенант (config/tenants/demo-garden.yaml) — другой брендинг на том же ядре.
export const GARDEN_WIDGET_KEY = "wk_e2e_demo_garden";
// Мок OpenAI (e2e/mock-llm.mjs): демо-тенант на provider openai — ходит в Responses API мока.
export const MOCK_LLM_PORT = 8002;
export const MOCK_LLM_URL = `http://${HOST}:${MOCK_LLM_PORT}`;

export const E2E_ENV: Record<string, string> = {
  APP_ENV: "test",
  DATABASE_URL:
    process.env.E2E_DATABASE_URL ?? "postgresql+asyncpg://app:app@localhost:5432/app_e2e",
  CORS_ORIGINS: JSON.stringify([WEB_URL]),
  OPENAI_API_KEY: "sk-e2e",
  OPENAI_BASE_URL: `${MOCK_LLM_URL}/v1`,
  // Лимиты частоты выключены (ADR-0030): e2e делают десятки ходов в минуту с одного IP,
  // а Redis-счётчики общие с dev-API. Сами лимиты проверяет pytest.
  RATE_TURNS_PER_IP_PER_MIN: "0",
  RATE_TURNS_PER_TENANT_PER_MIN: "0",
  RATE_CONVERSATIONS_PER_IP_PER_HOUR: "0",
  // Трейсинг выключен (ADR-0029): иначе ключи Langfuse из корневого .env отправили бы
  // трейсы e2e-диалогов в проект Langfuse.
  LANGFUSE_PUBLIC_KEY: "",
  LANGFUSE_SECRET_KEY: "",
};
