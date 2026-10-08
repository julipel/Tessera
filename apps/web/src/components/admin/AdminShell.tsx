"use client";

import Link from "next/link";
import type { ReactNode } from "react";
import { type AdminSession, useAdminSession } from "@/lib/admin/useAdminSession";

/** Каркас страниц админки: проверка входа, шапка с пользователем и «Выйти». */
export function AdminShell({
  apiUrl,
  children,
}: {
  apiUrl: string;
  children: (session: AdminSession) => ReactNode;
}) {
  const { session, error } = useAdminSession(apiUrl);

  return (
    <div className="min-h-dvh bg-chat-bg text-chat-text">
      <header className="border-b border-chat-border bg-chat-surface px-4 py-3">
        <div className="mx-auto flex max-w-5xl items-center gap-4">
          <Link href="/admin" className="font-semibold">
            Админка
          </Link>
          <span className="flex-1" />
          {session && (
            <>
              <span className="text-sm text-chat-muted">{session.me.email}</span>
              <button type="button" onClick={() => void session.logout()} className={secondaryButton}>
                Выйти
              </button>
            </>
          )}
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">
        {error && (
          <p role="alert" className="text-chat-danger">
            Не удалось загрузить данные: {error}
          </p>
        )}
        {session ? children(session) : !error && <p className="text-chat-muted">Загрузка…</p>}
      </main>
    </div>
  );
}

export const primaryButton =
  "rounded-chat bg-chat-primary px-4 py-2 text-sm font-medium text-chat-on-primary disabled:opacity-50";
export const secondaryButton =
  "rounded-chat border border-chat-border bg-chat-surface px-3 py-1.5 text-sm hover:bg-chat-bg disabled:opacity-50";
