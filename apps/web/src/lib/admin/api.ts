// HTTP API админки (docs/contracts.md §7): вход по email и паролю, дальше — сессия в
// HttpOnly-cookie, которую ставит API (ADR-0044): скрипт страницы токена не видит, запросы
// идут с `credentials: "include"`. Ошибки — ApiError, как у API чата.
import type {
  AdminLoginRequest,
  AdminLoginResponse,
  AdminMe,
  ConversationDetail,
  ConversationList,
  AgentConfigVersionDetail,
  AgentEventItem,
  AgentEventList,
  AgentConfigVersionList,
  CreateAgentConfigRequest,
  CreateSourceRequest,
  MessageRating,
  RateMessageRequest,
  SourceDetail,
  SourceFile,
  SourceFileList,
  SourceList,
  SourceSyncItem,
  SourceSyncList,
  StartSyncRequest,
} from "@/contracts";
import { ApiError, errorBody } from "@/lib/api/client";

/** Журнал хода в админке: размер страницы и сколько страниц грузить за раз. */
const EVENT_PAGE_SIZE = 1000;
const MAX_EVENT_PAGES = 20;

/** Вход: API ставит cookie сессии и возвращает текущего пользователя. Неверные данные —
 * ApiError 401, лимит — 429. */
export async function login(apiUrl: string, request: AdminLoginRequest): Promise<AdminLoginResponse> {
  const resp = await fetch(new URL("/v1/admin/auth/login", apiUrl), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!resp.ok) throw new ApiError(resp.status, await errorBody(resp));
  return (await resp.json()) as AdminLoginResponse;
}

export class AdminApi {
  constructor(private readonly baseUrl: string) {}

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

  /** Диалоги от новых к старым; `before` — `next_before` предыдущей страницы. */
  async listConversations(
    tenantId: string,
    options: { before?: string | null; rating?: "good" | "bad" | null; limit?: number } = {},
  ): Promise<ConversationList> {
    const query = new URLSearchParams();
    if (options.before) query.set("before", options.before);
    if (options.rating) query.set("rating", options.rating);
    if (options.limit) query.set("limit", String(options.limit));
    const suffix = query.size > 0 ? `?${query}` : "";
    return (await this.request(`${conversationsPath(tenantId)}${suffix}`)).json() as Promise<ConversationList>;
  }

  async getConversation(tenantId: string, conversationId: string): Promise<ConversationDetail> {
    const resp = await this.request(conversationsPath(tenantId, conversationId));
    return (await resp.json()) as ConversationDetail;
  }

  /**
   * Журнал хода диалога по времени событий: страницы подряд, начиная с `after`, но не больше
   * MAX_EVENT_PAGES за вызов; `nextAfter` — курсор продолжения, null — журнал загружен весь.
   */
  async listEvents(
    tenantId: string,
    conversationId: string,
    after: string | null = null,
  ): Promise<{ events: AgentEventItem[]; nextAfter: string | null }> {
    const events: AgentEventItem[] = [];
    for (let page = 0; page < MAX_EVENT_PAGES; page++) {
      const query = new URLSearchParams({ conversation_id: conversationId, limit: String(EVENT_PAGE_SIZE) });
      if (after) query.set("after", after);
      const resp = await this.request(`/v1/admin/tenants/${encodeURIComponent(tenantId)}/events?${query}`);
      const list = (await resp.json()) as AgentEventList;
      events.push(...list.events);
      after = list.next_after;
      if (!after) break;
    }
    return { events, nextAfter: after };
  }

  /** Оценка ответа ассистента; повторная заменяет прежнюю. */
  async rateMessage(
    tenantId: string,
    conversationId: string,
    messageId: string,
    request: RateMessageRequest,
  ): Promise<MessageRating> {
    const resp = await this.request(ratingPath(tenantId, conversationId, messageId), {
      method: "PUT",
      body: JSON.stringify(request),
    });
    return (await resp.json()) as MessageRating;
  }

  async clearRating(tenantId: string, conversationId: string, messageId: string): Promise<void> {
    await this.request(ratingPath(tenantId, conversationId, messageId), { method: "DELETE" });
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const resp = await fetch(new URL(path, this.baseUrl), {
      ...init,
      credentials: "include",
      headers: { "Content-Type": "application/json", ...init.headers },
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

function conversationsPath(tenantId: string, conversationId?: string): string {
  const base = `/v1/admin/tenants/${encodeURIComponent(tenantId)}/conversations`;
  return conversationId ? `${base}/${encodeURIComponent(conversationId)}` : base;
}

function ratingPath(tenantId: string, conversationId: string, messageId: string): string {
  return `${conversationsPath(tenantId, conversationId)}/messages/${encodeURIComponent(messageId)}/rating`;
}
