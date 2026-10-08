"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { AdminMe } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import { AdminApi, loadToken, saveToken } from "./api";

export const LOGIN_PATH = "/admin/login";

export interface AdminSession {
  api: AdminApi;
  me: AdminMe;
  /** Закрыть сессию и уйти на вход. */
  logout: () => Promise<void>;
  /** Сессия истекла или закрыта (401 любого запроса) — на вход. */
  expire: () => void;
}

/**
 * Сессия админки из сохранённого токена. Нет токена или он не принят — переход на вход;
 * пока `/v1/admin/me` не ответил — null. `error` — сбой загрузки, не связанный со входом.
 */
export function useAdminSession(apiUrl: string): { session: AdminSession | null; error: string | null } {
  const router = useRouter();
  const [token] = useState(loadToken);
  const api = useMemo(() => (token ? new AdminApi(apiUrl, token) : null), [apiUrl, token]);
  const [me, setMe] = useState<AdminMe | null>(null);
  const [error, setError] = useState<string | null>(null);

  const expire = useCallback(() => {
    saveToken(null);
    router.replace(LOGIN_PATH);
  }, [router]);

  useEffect(() => {
    if (!api) {
      router.replace(LOGIN_PATH);
      return;
    }
    api
      .me()
      .then(setMe)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) expire();
        else setError(e instanceof Error ? e.message : String(e));
      });
  }, [api, expire, router]);

  const logout = useCallback(async () => {
    try {
      await api?.logout();
    } catch {
      // Сессия уже закрыта или сеть недоступна — токен всё равно забываем.
    }
    expire();
  }, [api, expire]);

  const session = useMemo(
    () => (api && me ? { api, me, logout, expire } : null),
    [api, me, logout, expire],
  );
  return { session, error };
}
