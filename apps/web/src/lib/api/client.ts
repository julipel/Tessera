// Публичный HTTP API чата (docs/contracts.md §1). Ошибки — HttpError (`http_error.schema.json`).
import type {
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

  async createConversation(visitorId: string): Promise<string> {
    const resp = await this.request("/v1/conversations", {
      method: "POST",
      body: JSON.stringify({ visitor_id: visitorId }),
    });
    const body = (await resp.json()) as CreateConversationResponse;
    return body.conversation_id;
  }

  async getPublicConfig(): Promise<PublicConfig> {
    const resp = await this.request("/v1/public/config");
    return (await resp.json()) as PublicConfig;
  }

  async getHistory(conversationId: string): Promise<MessageHistory> {
    const resp = await this.request(`/v1/conversations/${conversationId}/messages`);
    return (await resp.json()) as MessageHistory;
  }

  /** Стрим событий хода. Ошибки ввода и доступа — ApiError до первого события. */
  async sendMessage(
    conversationId: string,
    request: SendMessageRequest,
    signal?: AbortSignal,
  ): Promise<AsyncGenerator<Event>> {
    const resp = await this.request(`/v1/conversations/${conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify(request),
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

async function errorBody(resp: Response): Promise<HttpErrorBody> {
  try {
    const body = (await resp.json()) as { error?: HttpErrorBody };
    if (body.error) return body.error;
  } catch {
    // не JSON — например, ответ прокси
  }
  return { code: "internal", message: `HTTP ${resp.status}`, retryable: resp.status >= 500 };
}
