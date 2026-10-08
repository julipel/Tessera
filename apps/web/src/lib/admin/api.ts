// HTTP API админки (docs/contracts.md §7): вход по email и паролю, дальше — токен сессии
// в `Authorization: Bearer`. Ошибки — ApiError, как у API чата.
import type {
  AdminLoginRequest,
  AdminLoginResponse,
  AdminMe,
  AgentConfigVersionDetail,
  AgentConfigVersionList,
  CreateAgentConfigRequest,
} from "@/contracts";
import { ApiError, errorBody } from "@/lib/api/client";

const TOKEN_KEY = "tessera:admin-token";

// localStorage может быть недоступен (приватный режим, запрет cookies) — тогда вход живёт
// до перезагрузки страницы.
export function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // см. loadToken
  }
}

/** Вход: токен сессии и текущий пользователь. Неверные данные — ApiError 401, лимит — 429. */
export async function login(apiUrl: string, request: AdminLoginRequest): Promise<AdminLoginResponse> {
  const resp = await fetch(new URL("/v1/admin/auth/login", apiUrl), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!resp.ok) throw new ApiError(resp.status, await errorBody(resp));
  return (await resp.json()) as AdminLoginResponse;
}

export class AdminApi {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
  ) {}

  async me(): Promise<AdminMe> {
    return (await this.request("/v1/admin/me")).json() as Promise<AdminMe>;
  }

  async logout(): Promise<void> {
    await this.request("/v1/admin/auth/logout", { method: "POST" });
  }

  /** Версии конфига от новой к старой. */
  async listConfigs(tenantId: string): Promise<AgentConfigVersionList> {
    return (await this.request(configsPath(tenantId))).json() as Promise<AgentConfigVersionList>;
  }

  async getConfig(tenantId: string, configId: string): Promise<AgentConfigVersionDetail> {
    const resp = await this.request(`${configsPath(tenantId)}/${configId}`);
    return (await resp.json()) as AgentConfigVersionDetail;
  }

  /** Новый черновик. Конфиг не прошёл проверку — ApiError 422 с `details` по местам. */
  async createConfig(tenantId: string, yaml: string): Promise<AgentConfigVersionDetail> {
    const request: CreateAgentConfigRequest = { yaml };
    const resp = await this.request(configsPath(tenantId), { method: "POST", body: JSON.stringify(request) });
    return (await resp.json()) as AgentConfigVersionDetail;
  }

  /** Черновик или архивная версия (откат) становится активной. */
  async activateConfig(tenantId: string, configId: string): Promise<AgentConfigVersionDetail> {
    const resp = await this.request(`${configsPath(tenantId)}/${configId}/activate`, { method: "POST" });
    return (await resp.json()) as AgentConfigVersionDetail;
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const resp = await fetch(new URL(path, this.baseUrl), {
      ...init,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${this.token}`,
        ...init.headers,
      },
    });
    if (!resp.ok) throw new ApiError(resp.status, await errorBody(resp));
    return resp;
  }
}

function configsPath(tenantId: string): string {
  return `/v1/admin/tenants/${encodeURIComponent(tenantId)}/agent-configs`;
}
