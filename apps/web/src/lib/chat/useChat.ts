"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { ApiError, ChatApi } from "@/lib/api/client";
import { type ChatMessage, chatReducer, initialChatState } from "./state";

const VISITOR_KEY = "tessera:visitor_id";
const conversationKey = (widgetKey: string) => `tessera:conversation:${widgetKey}`;

export interface UseChat {
  messages: ChatMessage[];
  busy: boolean;
  activity: string | null;
  loadError: string | null;
  send: (text: string) => Promise<void>;
}

/** Диалог посетителя: восстановление из истории, отправка и стрим хода. */
export function useChat(apiUrl: string, widgetKey: string): UseChat {
  const api = useMemo(() => new ChatApi(apiUrl, widgetKey), [apiUrl, widgetKey]);
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [loadError, setLoadError] = useState<string | null>(null);
  const conversationId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const loadHistory = useCallback(
    async (id: string) => dispatch({ type: "history", history: await api.getHistory(id) }),
    [api],
  );

  useEffect(() => {
    const stored = readStorage(conversationKey(widgetKey));
    conversationId.current = stored;
    if (stored) {
      loadHistory(stored).catch((e: unknown) => {
        if (e instanceof ApiError && e.code === "conversation_not_found") {
          // Диалог удалён или ключ сменился — начнём новый при первой отправке.
          conversationId.current = null;
          writeStorage(conversationKey(widgetKey), null);
        } else {
          setLoadError(errorText(e));
        }
      });
    }
    return () => abort.current?.abort();
  }, [widgetKey, loadHistory]);

  const send = useCallback(
    async (text: string) => {
      if (state.busy || !text.trim()) return;
      const clientMessageId = crypto.randomUUID();
      dispatch({ type: "user_sent", id: `local:${clientMessageId}`, text });
      abort.current = new AbortController();
      let finished = false;
      try {
        if (!conversationId.current) {
          conversationId.current = await api.createConversation(visitorId());
          writeStorage(conversationKey(widgetKey), conversationId.current);
        }
        const events = await api.sendMessage(
          conversationId.current,
          { client_message_id: clientMessageId, input: { type: "text", text } },
          abort.current.signal,
        );
        for await (const event of events) {
          dispatch({ type: "event", event });
          if (event.type === "done") finished = true;
        }
      } catch (e) {
        if (e instanceof ApiError || !conversationId.current) {
          dispatch({ type: "stream_failed", message: errorText(e) });
          return;
        }
        // Обрыв сети — дальше как при стриме без `done`.
      }
      if (!finished && conversationId.current && !abort.current.signal.aborted) {
        // Стрим оборвался: ответ (возможно, частичный) уже в истории — берём оттуда.
        await loadHistory(conversationId.current).catch((e: unknown) =>
          dispatch({ type: "stream_failed", message: errorText(e) }),
        );
      }
    },
    [api, widgetKey, state.busy, loadHistory],
  );

  return { ...state, loadError, send };
}

function visitorId(): string {
  let id = readStorage(VISITOR_KEY);
  if (!id) {
    id = crypto.randomUUID();
    writeStorage(VISITOR_KEY, id);
  }
  return id;
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  return "Нет связи с сервером. Попробуйте ещё раз.";
}

// localStorage может быть недоступен (приватный режим, iframe со сторонними cookie).
function readStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // без сохранения диалог живёт до перезагрузки страницы
  }
}
