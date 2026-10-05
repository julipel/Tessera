import { execFileSync } from "node:child_process";
import { E2E_ENV, GARDEN_WIDGET_KEY, WIDGET_KEY } from "./env";

/** База e2e: создать, применить миграции, засеять демо-тенантов с известными ключами виджета. */
export default function globalSetup(): void {
  const run = (...args: string[]) =>
    execFileSync("uv", ["run", ...args], {
      cwd: "../api",
      env: { ...process.env, ...E2E_ENV },
      stdio: "inherit",
    });
  run("python", "-m", "app.cli", "ensure-db");
  run("alembic", "upgrade", "head");
  const keys = `demo-beauty=${WIDGET_KEY},demo-garden=${GARDEN_WIDGET_KEY}`;
  run("python", "-m", "app.cli", "seed", "--widget-key", keys, "--reset-widget-key");
}
