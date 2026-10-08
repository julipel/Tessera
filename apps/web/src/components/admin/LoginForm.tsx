"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";
import { login, saveToken } from "@/lib/admin/api";
import { ApiError } from "@/lib/api/client";
import { primaryButton } from "./AdminShell";

export function LoginForm({ apiUrl }: { apiUrl: string }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token } = await login(apiUrl, { email, password });
      saveToken(token);
      router.replace("/admin");
    } catch (e) {
      setError(loginError(e));
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center bg-chat-bg px-4 text-chat-text">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-chat border border-chat-border bg-chat-surface p-6"
      >
        <h1 className="text-lg font-semibold">Вход в админку</h1>
        <label className="block text-sm">
          Email
          <input
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className={input}
          />
        </label>
        <label className="block text-sm">
          Пароль
          <input
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className={input}
          />
        </label>
        {error && (
          <p role="alert" className="text-sm text-chat-danger">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy} className={`${primaryButton} w-full`}>
          Войти
        </button>
      </form>
    </main>
  );
}

const input =
  "mt-1 block w-full rounded-chat border border-chat-border px-3 py-2 focus:outline-2 focus:outline-chat-primary";

function loginError(e: unknown): string {
  if (e instanceof ApiError) {
    // Причину 401 сервер не уточняет (ADR-0036) — и мы не уточняем.
    if (e.status === 401) return "Неверный email или пароль";
    if (e.status === 429) return "Слишком много попыток входа, попробуйте позже";
    return e.message;
  }
  return "Сервер недоступен, попробуйте позже";
}
