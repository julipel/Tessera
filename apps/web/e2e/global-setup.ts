import { execFileSync } from "node:child_process";
import path from "node:path";
import {
  ADMIN_EMAIL,
  ADMIN_PASSWORD,
  ADMIN_TENANT_SLUG,
  E2E_ENV,
  GARDEN_WIDGET_KEY,
  WIDGET_KEY,
} from "./env";

/**
 * База e2e: создать, применить миграции, засеять демо-тенантов с известными ключами виджета,
 * тенанта админки и его editor (пароль сбрасывается на каждом запуске).
 */
export default function globalSetup(): void {
  const run = (...args: string[]) =>
    execFileSync("uv", ["run", ...args], {
      cwd: "../api",
      // ADMIN_PASSWORD читает только `admin-user`.
      env: { ...process.env, ...E2E_ENV, ADMIN_PASSWORD },
      stdio: "inherit",
    });
  run("python", "-m", "app.cli", "ensure-db");
  run("alembic", "upgrade", "head");
  const keys = `demo-beauty=${WIDGET_KEY},demo-garden=${GARDEN_WIDGET_KEY}`;
  run("python", "-m", "app.cli", "seed", "--widget-key", keys, "--reset-widget-key");
  run("python", "-m", "app.cli", "seed", path.resolve("e2e/fixtures/admin-tenant.yaml"));
  run("python", "-m", "app.cli", "admin-user", ADMIN_EMAIL, "--tenant", `${ADMIN_TENANT_SLUG}=editor`, "--reset-password");
}
