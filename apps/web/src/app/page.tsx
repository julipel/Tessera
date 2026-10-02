import { Chat } from "@/components/chat/Chat";

// Полноэкранный чат. Ключ виджета — `?key=` (так его передаст iframe виджета в P5-06),
// для локальной разработки — NEXT_PUBLIC_WIDGET_KEY.
export default async function Home({ searchParams }: PageProps<"/">) {
  const { key } = await searchParams;
  const widgetKey = (typeof key === "string" && key) || process.env.NEXT_PUBLIC_WIDGET_KEY;
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

  if (!widgetKey) {
    return (
      <main className="p-8">
        <h1 className="text-xl font-semibold">AI-консультант</h1>
        <p role="alert" className="mt-2 text-chat-danger">
          Не задан ключ виджета: откройте страницу с параметром <code>?key=…</code>.
        </p>
      </main>
    );
  }
  return <Chat apiUrl={apiUrl} widgetKey={widgetKey} />;
}
