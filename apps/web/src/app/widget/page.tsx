import { WidgetChat } from "@/components/widget/WidgetChat";

// Страница iframe виджета (public/widget.js). CSP frame-ancestors по ключу ставит src/proxy.ts.
export default async function Widget({ searchParams }: PageProps<"/widget">) {
  const { key } = await searchParams;
  const apiUrl = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

  if (typeof key !== "string" || !key) {
    return (
      <p role="alert" className="p-4 text-chat-danger">
        Не задан ключ виджета.
      </p>
    );
  }
  return <WidgetChat apiUrl={apiUrl} widgetKey={key} />;
}
