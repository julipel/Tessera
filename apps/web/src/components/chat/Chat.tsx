"use client";

import { brandingStyle, logoUrl } from "@/lib/branding";
import { useChat } from "@/lib/chat/useChat";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

export function Chat({ apiUrl, widgetKey }: { apiUrl: string; widgetKey: string }) {
  const { messages, busy, activity, loadError, config, send, sendText, act, submitForm } = useChat(
    apiUrl,
    widgetKey,
  );
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
          <h1 className="text-lg font-semibold">{config?.assistant.name ?? "AI-консультант"}</h1>
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
        greeting={config?.assistant.greeting}
        starter={config?.assistant.starter_suggestions ?? []}
        onAction={idle ? (action, subject) => void act(action, subject) : undefined}
        onSubmitForm={idle ? (form, values) => void submitForm(form, values) : undefined}
        onPick={idle ? (input) => void send(input) : undefined}
      />
      <Composer disabled={busy} onSend={(text) => void sendText(text)} />
    </main>
  );
}
