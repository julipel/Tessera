"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Action, Form, PublicConfig, UserInput } from "@/contracts";
import { ApiError, ChatApi } from "@/lib/api/client";
import { type ChatMessage, chatReducer, initialChatState } from "./state";

const VISITOR_KEY = "tessera:visitor_id";
const conversationKey = (widgetKey: string) => `tessera:conversation:${widgetKey}`;
// Причина abort стрима при размонтировании: историю тогда не загружаем.
const UNMOUNT = "unmount";

export interface UseChat {
  messages: ChatMessage[];
  busy: boolean;
  activity: string | null;
  restoring: boolean;
  loadError: string | null;
  /** Публичный конфиг тенанта: имя, приветствие, стартовые подсказки, брендинг. */
  config: PublicConfig | null;
  send: (input: UserInput) => Promise<void>;
  sendText: (text: string) => Promise<void>;
  /** Нажатие кнопки компонента: `input.type=action` с подписью выбора для истории. */
  act: (action: Action, subject?: string) => Promise<void>;
  /** Отправка формы: `input.type=form_submit`, в истории — заголовок формы. */
  submitForm: (form: Form, values: Record<string, string>) => Promise<void>;
  /** Прервать идущий ход; null — прерывать нечего (ход ещё не начат или не идёт). */
  stop: (() => Promise<void>) | null;
  /** Повторить последний ввод после ошибки, которую можно повторить; иначе null. */
  retry: (() => Promise<void>) | null;
}

const LABEL_MAX = 200; // ActionInput.label и FormSubmitInput.label в user_input.schema.json

/** Диалог посетителя: восстановление из истории, отправка и стрим хода. */
export function useChat(apiUrl: string, widgetKey: string): UseChat {
  const api = useMemo(() => new ChatApi(apiUrl, widgetKey), [apiUrl, widgetKey]);
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const conversationId = useRef<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const loadHistory = useCallback(
    async (id: string) => dispatch({ type: "history", history: await api.getHistory(id) }),
    [api],
  );

  useEffect(() => {
    // Без конфига чат работает: нейтральная тема, без приветствия и стартовых подсказок.
    api
      .getPublicConfig()
      .then(setConfig)
      .catch(() => setConfig(null));
  }, [api]);

  useEffect(() => {
    const stored = readStorage(conversationKey(widgetKey));
    conversationId.current = stored;
    if (stored) {
      dispatch({ type: "restoring", value: true });
      loadHistory(stored).catch((e: unknown) => {
        dispatch({ type: "restoring", value: false });
        if (e instanceof ApiError && e.code === "conversation_not_found") {
          // Диалог удалён или ключ сменился — начнём новый при первой отправке.
          conversationId.current = null;
          writeStorage(conversationKey(widgetKey), null);
        } else {
          setLoadError(errorText(e));
        }
      });
    }
    return () => abort.current?.abort(UNMOUNT);
  }, [widgetKey, loadHistory]);

  const send = useCallback(
    async (input: UserInput) => {
      if (state.busy || (input.type === "text" && !input.text.trim())) return;
      const clientMessageId = crypto.randomUUID();
      dispatch({ type: "user_sent", id: `local:${clientMessageId}`, input });
      abort.current = new AbortController();
      let finished = false;
      try {
        if (!conversationId.current) {
          conversationId.current = await api.createConversation(visitorId());
          writeStorage(conversationKey(widgetKey), conversationId.current);
        }
        const events = await api.sendMessage(
          conversationId.current,
          { client_message_id: clientMessageId, input },
          abort.current.signal,
        );
        for await (const event of events) {
          dispatch({ type: "event", event });
          if (event.type === "done") finished = true;
        }
      } catch (e) {
        if (e instanceof ApiError || !conversationId.current) {
          dispatch({ type: "stream_failed", message: errorText(e), retryable: isRetryable(e) });
          return;
        }
        // Обрыв сети или стрим закрыт кнопкой «Остановить» — дальше как при стриме без `done`.
      }
      if (!finished && conversationId.current && abort.current.signal.reason !== UNMOUNT) {
        // Стрим оборвался: ответ (возможно, частичный) уже в истории — берём оттуда.
        await loadHistory(conversationId.current).catch((e: unknown) =>
          dispatch({ type: "stream_failed", message: errorText(e), retryable: isRetryable(e) }),
        );
      }
    },
    [api, widgetKey, state.busy, loadHistory],
  );

  const sendText = useCallback((text: string) => send({ type: "text", text }), [send]);

  const act = useCallback(
    (action: Action, subject?: string) =>
      send({
        type: "action",
        action_id: action.action_id,
        label: (subject ? `${action.label} — ${subject}` : action.label).slice(0, LABEL_MAX),
        payload: action.payload ?? {},
      }),
    [send],
  );

  const submitForm = useCallback(
    (form: Form, values: Record<string, string>) =>
      send({
        type: "form_submit",
        form_id: form.form_id,
        label: form.title.slice(0, LABEL_MAX) || undefined,
        values,
      }),
    [send],
  );

  const turnId = state.busy ? state.messages.find((m) => m.status === "streaming")?.turnId : undefined;
  const stopTurn = useCallback(async () => {
    const id = conversationId.current;
    if (!id || !turnId) return;
    try {
      await api.cancelTurn(id, turnId);
    } catch (e) {
      // 404 — ход уже закончился, `done` придёт в стрим. Без связи — закрываем стрим сами:
      // бэкенд прервёт ход и сохранит частичный ответ, его возьмём из истории.
      if (!(e instanceof ApiError)) abort.current?.abort();
    }
  }, [api, turnId]);

  const last = state.messages.at(-1);
  const retryInput = state.messages.findLast((m) => m.role === "user")?.input;
  const retryLast = useCallback(async () => {
    if (!retryInput) return;
    dispatch({ type: "retry" });
    await send(retryInput);
  }, [send, retryInput]);

  return {
    messages: state.messages,
    busy: state.busy,
    activity: state.activity,
    restoring: state.restoring,
    loadError,
    config,
    send,
    sendText,
    act,
    submitForm,
    stop: turnId ? stopTurn : null,
    retry: !state.busy && last?.retryable && retryInput ? retryLast : null,
  };
}

function visitorId(): string {
  let id = readStorage(VISITOR_KEY);
  if (!id) {
    id = crypto.randomUUID();
    writeStorage(VISITOR_KEY, id);
  }
  return id;
}

/** Сеть — повторяемо; HTTP-ошибка — по `retryable` из ответа. */
function isRetryable(e: unknown): boolean {
  return e instanceof ApiError ? e.body.retryable : true;
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
