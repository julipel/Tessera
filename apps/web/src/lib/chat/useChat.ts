"use client";

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import type { Action, Event, Form, PublicConfig, UserInput } from "@/contracts";
import { ApiError, ChatApi } from "@/lib/api/client";
import { type ChatMessage, chatReducer, initialChatState, retryTarget } from "./state";

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
  /** Повторить после ошибки, которую можно повторить (неудачный ответ или ввод без ответа); иначе null. */
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
  const failStream = useCallback(
    (e: unknown) => dispatch({ type: "stream_failed", message: errorText(e), retryable: isRetryable(e) }),
    [],
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

  /**
   * Читает стрим хода в состояние. `open` открывает стрим (создав диалог, если его ещё нет);
   * ApiError до первого события передаётся в `onRejected`. Стрим оборвался без `done` —
   * ответ (возможно, частичный) уже в истории на сервере: берём оттуда.
   */
  const runTurn = useCallback(
    async (
      open: (conversation: string, signal: AbortSignal) => Promise<AsyncGenerator<Event>>,
      onRejected: (e: unknown) => Promise<void> | void,
    ) => {
      abort.current = new AbortController();
      let finished = false;
      try {
        if (!conversationId.current) {
          conversationId.current = await api.createConversation(visitorId());
          writeStorage(conversationKey(widgetKey), conversationId.current);
        }
        for await (const event of await open(conversationId.current, abort.current.signal)) {
          dispatch({ type: "event", event });
          if (event.type === "done") finished = true;
        }
      } catch (e) {
        if (e instanceof ApiError || !conversationId.current) {
          await onRejected(e);
          return;
        }
        // Обрыв сети или стрим закрыт кнопкой «Остановить» — дальше как при стриме без `done`.
      }
      if (!finished && conversationId.current && abort.current.signal.reason !== UNMOUNT) {
        await loadHistory(conversationId.current).catch(failStream);
      }
    },
    [api, widgetKey, loadHistory, failStream],
  );

  const send = useCallback(
    async (input: UserInput) => {
      if (state.busy || (input.type === "text" && !input.text.trim())) return;
      const clientMessageId = crypto.randomUUID();
      dispatch({ type: "user_sent", id: `local:${clientMessageId}`, input });
      await runTurn(
        (conversation, signal) =>
          api.sendMessage(conversation, { client_message_id: clientMessageId, input }, signal),
        failStream,
      );
    },
    [api, state.busy, runTurn, failStream],
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

  const target = state.busy ? null : retryTarget(state.messages);
  const retry = useCallback(async () => {
    if (!target) return;
    dispatch({ type: "retry" });
    if (target.kind === "resend") {
      await send(target.input);
      return;
    }
    // Новый ход вместо неудачного ответа (ADR-0023). Отказ (уже повторён, не последний,
    // сбой до стрима) — неудачный ответ из ленты уже убран: верным состоянием будет история.
    await runTurn(
      (conversation, signal) => api.retryMessage(conversation, target.messageId, signal),
      async (e) => {
        const id = conversationId.current;
        if (!id) return failStream(e);
        await loadHistory(id).catch(failStream);
      },
    );
  }, [api, target, send, runTurn, loadHistory, failStream]);

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
    retry: target ? retry : null,
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
