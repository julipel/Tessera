"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { AdminMe } from "@/contracts";
import { ApiError } from "@/lib/api/client";
import { AdminApi } from "./api";

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
 * Сессия админки — HttpOnly-cookie API (ADR-0044): есть ли она, знает только сервер, поэтому
 * первым идёт `/v1/admin/me`. 401 — переход на вход; пока ответа нет — null. `error` — сбой
 * загрузки, не связанный со входом.
 */
export function useAdminSession(apiUrl: string): {
  session: AdminSession | null;
  error: string | null;
  /** Выход не выполнен (сбой сети или сервера): сессия жива, на вход не уходим. */
  logoutError: string | null;
} {
  const router = useRouter();
  const api = useMemo(() => new AdminApi(apiUrl), [apiUrl]);
  const [me, setMe] = useState<AdminMe | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [logoutError, setLogoutError] = useState<string | null>(null);

  const expire = useCallback(() => {
    router.replace(LOGIN_PATH);
  }, [router]);

  useEffect(() => {
    api
      .me()
      .then(setMe)
      .catch((e: unknown) => {
        if (e instanceof ApiError && e.status === 401) expire();
        else setError(e instanceof Error ? e.message : String(e));
      });
  }, [api, expire]);

  const logout = useCallback(async () => {
    setLogoutError(null);
    try {
      await api.logout();
    } catch (e) {
      // Cookie HttpOnly — стереть её может только сервер. Не вышло — пользователь должен это
      // увидеть, иначе на общем компьютере админка откроется снова без пароля. 401 — сессии
      // уже нет, это и есть выход.
      if (!(e instanceof ApiError && e.status === 401)) {
        setLogoutError(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    expire();
  }, [api, expire]);

  const session = useMemo(
    () => (me ? { api, me, logout, expire } : null),
    [api, me, logout, expire],
  );
  return { session, error, logoutError };
}
