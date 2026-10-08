// Публичный HTTP API чата (docs/contracts.md §1). Ошибки — HttpError (`http_error.schema.json`).
import type {
  CreateConversationRequest,
  CreateConversationResponse,
  Event,
  HttpErrorBody,
  MessageHistory,
  PublicConfig,
  SendMessageRequest,
} from "@/contracts";
import { readEvents } from "./sse";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: HttpErrorBody,
  ) {
    super(body.message);
    this.name = "ApiError";
  }

  get code(): HttpErrorBody["code"] {
    return this.body.code;
  }
}

export class ChatApi {
  constructor(
    private readonly baseUrl: string,
    private readonly widgetKey: string,
  ) {}

  /** `locale` — язык клиента (BCP 47): по нему бэкенд выбирает язык диалога (ADR-0025). */
  async createConversation(visitorId: string, locale?: string): Promise<string> {
    const request: CreateConversationRequest = { visitor_id: visitorId, locale };
    const resp = await this.request("/v1/conversations", {
      method: "POST",
      body: JSON.stringify(request),
    });
    const body = (await resp.json()) as CreateConversationResponse;
    return body.conversation_id;
  }

  /** Конфиг на языке, который получит диалог с этим `locale`. */
  async getPublicConfig(locale?: string): Promise<PublicConfig> {
    const query = locale ? `?${new URLSearchParams({ locale })}` : "";
    const resp = await this.request(`/v1/public/config${query}`);
    return (await resp.json()) as PublicConfig;
  }

  async getHistory(conversationId: string): Promise<MessageHistory> {
    const resp = await this.request(`/v1/conversations/${conversationId}/messages`);
    return (await resp.json()) as MessageHistory;
  }

  /** Прервать ход: стрим закончится `done{interrupted}`. 404 — ход уже завершён. */
  async cancelTurn(conversationId: string, turnId: string): Promise<void> {
    await this.request(`/v1/conversations/${conversationId}/turns/${turnId}/cancel`, {
      method: "POST",
    });
  }

  /** Стрим событий хода. Ошибки ввода и доступа — ApiError до первого события. */
  sendMessage(
    conversationId: string,
    request: SendMessageRequest,
    signal?: AbortSignal,
  ): Promise<AsyncGenerator<Event>> {
    return this.stream(`/v1/conversations/${conversationId}/messages`, JSON.stringify(request), signal);
  }

  /**
   * Повтор неудачного ответа `messageId` (ADR-0023): стрим нового хода по тому же вводу.
   * Ответ нельзя повторить — ApiError `not_retryable` (409) или `not_found` (404).
   */
  retryMessage(conversationId: string, messageId: string, signal?: AbortSignal): Promise<AsyncGenerator<Event>> {
    return this.stream(`/v1/conversations/${conversationId}/messages/${messageId}/retry`, undefined, signal);
  }

  private async stream(path: string, body: string | undefined, signal?: AbortSignal): Promise<AsyncGenerator<Event>> {
    const resp = await this.request(path, {
      method: "POST",
      body,
      headers: { Accept: "text/event-stream" },
      signal,
    });
    if (!resp.body) throw new Error("пустой ответ стрима");
    return readEvents(resp.body);
  }

  private async request(path: string, init: RequestInit = {}): Promise<Response> {
    const resp = await fetch(new URL(path, this.baseUrl), {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-Widget-Key": this.widgetKey,
        ...init.headers,
      },
    });
    if (!resp.ok) throw new ApiError(resp.status, await errorBody(resp));
    return resp;
  }
}

export async function errorBody(resp: Response): Promise<HttpErrorBody> {
  try {
    const body = (await resp.json()) as { error?: HttpErrorBody };
    if (body.error) return body.error;
  } catch {
    // не JSON — например, ответ прокси
  }
  return { code: "internal", message: `HTTP ${resp.status}`, retryable: resp.status >= 500 };
}
