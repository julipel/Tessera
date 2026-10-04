"use client";

import { useChat } from "@/lib/chat/useChat";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

export function Chat({ apiUrl, widgetKey }: { apiUrl: string; widgetKey: string }) {
  const { messages, busy, activity, loadError, starterSuggestions, send, sendText, act } = useChat(
    apiUrl,
    widgetKey,
  );
  // Во время хода кнопки и подсказки неактивны: ввод — один за раз.
  const idle = !busy;

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
      <MessageList
        messages={messages}
        activity={activity}
        starter={starterSuggestions}
        onAction={idle ? (action, subject) => void act(action, subject) : undefined}
        onPick={idle ? (input) => void send(input) : undefined}
      />
      <Composer disabled={busy} onSend={(text) => void sendText(text)} />
    </main>
  );
}
