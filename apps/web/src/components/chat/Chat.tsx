"use client";

import { useChat } from "@/lib/chat/useChat";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

export function Chat({ apiUrl, widgetKey }: { apiUrl: string; widgetKey: string }) {
  const { messages, busy, activity, loadError, send } = useChat(apiUrl, widgetKey);

  return (
    <main className="flex h-dvh flex-col">
      <header className="border-b border-chat-border bg-chat-surface px-4 py-3">
        <h1 className="mx-auto max-w-2xl text-lg font-semibold">AI-консультант</h1>
      </header>
      {loadError && (
        <p role="alert" className="px-4 py-2 text-center text-sm text-chat-danger">
          {loadError}
        </p>
      )}
      <MessageList messages={messages} activity={activity} />
      <Composer disabled={busy} onSend={(text) => void send(text)} />
    </main>
  );
}
