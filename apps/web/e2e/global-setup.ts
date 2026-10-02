import { execFileSync } from "node:child_process";
import { E2E_ENV, WIDGET_KEY } from "./env";

/** База e2e: создать, применить миграции, засеять демо-тенанта с известным ключом виджета. */
export default function globalSetup(): void {
  const run = (...args: string[]) =>
    execFileSync("uv", ["run", ...args], {
      cwd: "../api",
      env: { ...process.env, ...E2E_ENV },
      stdio: "inherit",
    });
  run("python", "-m", "app.cli", "ensure-db");
  run("alembic", "upgrade", "head");
  run("python", "-m", "app.cli", "seed", "--widget-key", WIDGET_KEY, "--reset-widget-key");
}
