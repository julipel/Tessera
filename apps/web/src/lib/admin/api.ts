// HTTP API админки (docs/contracts.md §7): вход по email и паролю, дальше — токен сессии
// в `Authorization: Bearer`. Ошибки — ApiError, как у API чата.
import type {
  AdminLoginRequest,
  AdminLoginResponse,
  AdminMe,
  AgentConfigVersionDetail,
  AgentConfigVersionList,
  CreateAgentConfigRequest,
  CreateSourceRequest,
  SourceDetail,
  SourceFile,
  SourceFileList,
  SourceList,
  SourceSyncItem,
  SourceSyncList,
  StartSyncRequest,
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

  /** Источники тенанта по имени, с последней синхронизацией. */
  async listSources(tenantId: string): Promise<SourceList> {
    return (await this.request(sourcesPath(tenantId))).json() as Promise<SourceList>;
  }

  async getSource(tenantId: string, sourceId: string): Promise<SourceDetail> {
    return (await this.request(sourcesPath(tenantId, sourceId))).json() as Promise<SourceDetail>;
  }

  /** Источник website/file/table. Не прошёл проверку — ApiError 422 с `details` по местам. */
  async createSource(tenantId: string, request: CreateSourceRequest): Promise<SourceDetail> {
    const resp = await this.request(sourcesPath(tenantId), { method: "POST", body: JSON.stringify(request) });
    return (await resp.json()) as SourceDetail;
  }

  /** Последние синхронизации, от новой к старой. */
  async listSyncs(tenantId: string, sourceId: string): Promise<SourceSyncList> {
    return (await this.request(`${sourcesPath(tenantId, sourceId)}/syncs`)).json() as Promise<SourceSyncList>;
  }

  /** Синхронизация в очереди воркера; уже ждущая или идущая возвращается та же. */
  async startSync(tenantId: string, sourceId: string, full: boolean): Promise<SourceSyncItem> {
    const request: StartSyncRequest = { full };
    const resp = await this.request(`${sourcesPath(tenantId, sourceId)}/sync`, {
      method: "POST",
      body: JSON.stringify(request),
    });
    return (await resp.json()) as SourceSyncItem;
  }

  async listFiles(tenantId: string, sourceId: string): Promise<SourceFileList> {
    return (await this.request(filesPath(tenantId, sourceId))).json() as Promise<SourceFileList>;
  }

  /** Тело — байты файла (без multipart); файл с тем же именем заменяется. */
  async uploadFile(tenantId: string, sourceId: string, file: File): Promise<SourceFile> {
    const resp = await this.request(filesPath(tenantId, sourceId, file.name), {
      method: "POST",
      body: file,
      headers: { "Content-Type": "application/octet-stream" },
    });
    return (await resp.json()) as SourceFile;
  }

  async deleteFile(tenantId: string, sourceId: string, name: string): Promise<void> {
    await this.request(filesPath(tenantId, sourceId, name), { method: "DELETE" });
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

function sourcesPath(tenantId: string, sourceId?: string): string {
  const base = `/v1/admin/tenants/${encodeURIComponent(tenantId)}/sources`;
  return sourceId ? `${base}/${encodeURIComponent(sourceId)}` : base;
}

function filesPath(tenantId: string, sourceId: string, name?: string): string {
  const base = `${sourcesPath(tenantId, sourceId)}/files`;
  return name ? `${base}/${encodeURIComponent(name)}` : base;
}
