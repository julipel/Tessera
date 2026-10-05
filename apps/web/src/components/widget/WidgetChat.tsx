"use client";

import { Chat } from "@/components/chat/Chat";

// Сообщение родительской странице (public/widget.js): свернуть панель виджета.
// Данных в нём нет, поэтому targetOrigin `*` — origin родителя фрейм достоверно не знает.
const COLLAPSE = { type: "tessera:collapse" } as const;

/** Чат во фрейме виджета: «Свернуть» скрывает iframe на сайте тенанта. */
export function WidgetChat({ apiUrl, widgetKey }: { apiUrl: string; widgetKey: string }) {
  return (
    <Chat
      apiUrl={apiUrl}
      widgetKey={widgetKey}
      onCollapse={() => window.parent.postMessage(COLLAPSE, "*")}
    />
  );
}
