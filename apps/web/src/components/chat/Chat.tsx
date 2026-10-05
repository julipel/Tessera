"use client";

import { brandingStyle, logoUrl } from "@/lib/branding";
import { useChat } from "@/lib/chat/useChat";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

export function Chat({
  apiUrl,
  widgetKey,
  onCollapse,
}: {
  apiUrl: string;
  widgetKey: string;
  /** Чат во фрейме виджета: кнопка «Свернуть» в шапке. */
  onCollapse?: () => void;
}) {
  const {
    messages,
    busy,
    activity,
    restoring,
    loadError,
    config,
    send,
    sendText,
    act,
    submitForm,
    stop,
    retry,
  } = useChat(apiUrl, widgetKey);
  // Во время хода кнопки, формы и подсказки неактивны: ввод — один за раз.
  const idle = !busy;
  // Пока конфиг не загружен (или недоступен) — нейтральная тема и общее название.
  const logo = logoUrl(config?.branding);

  return (
    <main
      className="flex h-dvh flex-col bg-chat-bg font-chat text-chat-text"
      style={brandingStyle(config?.branding)}
    >
      <header className="border-b border-chat-border bg-chat-surface px-4 py-3">
        <div className="mx-auto flex max-w-2xl items-center gap-3">
          {logo && (
            // Логотип с произвольного домена тенанта — next/image потребовал бы remotePatterns.
            // eslint-disable-next-line @next/next/no-img-element
            <img src={logo} alt="" className="size-8 shrink-0 object-contain" />
          )}
          <h1 className="flex-1 text-lg font-semibold">{config?.assistant.name ?? "AI-консультант"}</h1>
          {onCollapse && (
            <button
              type="button"
              onClick={onCollapse}
              aria-label="Свернуть"
              title="Свернуть"
              className="flex size-9 shrink-0 items-center justify-center rounded-chat text-chat-muted hover:bg-chat-bg focus:outline-2 focus:outline-chat-primary"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" className="size-5 fill-none stroke-current stroke-2">
                <path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
        </div>
      </header>
      {loadError && (
        <p role="alert" className="px-4 py-2 text-center text-sm text-chat-danger">
          {loadError}
        </p>
      )}
      <MessageList
        messages={messages}
        activity={activity}
        greeting={restoring ? undefined : config?.assistant.greeting}
        starter={restoring ? [] : (config?.assistant.starter_suggestions ?? [])}
        onAction={idle ? (action, subject) => void act(action, subject) : undefined}
        onSubmitForm={idle ? (form, values) => void submitForm(form, values) : undefined}
        onPick={idle ? (input) => void send(input) : undefined}
        onRetry={retry ? () => void retry() : undefined}
      />
      <Composer
        busy={busy}
        onSend={(text) => void sendText(text)}
        onStop={stop ? () => void stop() : undefined}
      />
    </main>
  );
}
