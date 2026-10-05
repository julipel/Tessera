import { notFound } from "next/navigation";
import Script from "next/script";

// Демо-«сайт магазина» со встроенным виджетом — для разработки и e2e (e2e/widget.spec.ts).
// Origin страницы должен быть в allowed_origins ключа, иначе iframe заблокирует CSP.
export default async function WidgetDemo({ searchParams }: PageProps<"/demo/widget">) {
  if (process.env.NODE_ENV === "production") notFound();
  const { key } = await searchParams;
  const widgetKey = (typeof key === "string" && key) || process.env.NEXT_PUBLIC_WIDGET_KEY;

  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-4 px-4 py-8">
      <h1 className="text-2xl font-semibold">Демо-магазин</h1>
      <p>
        Страница сайта тенанта. Виджет подключается одной строкой:{" "}
        <code className="break-all">{'<script src="…/widget.js" data-key="wk_…" async></script>'}</code>
      </p>
      {widgetKey ? (
        <Script src="/widget.js" data-key={widgetKey} strategy="afterInteractive" />
      ) : (
        <p role="alert" className="text-chat-danger">
          Не задан ключ виджета: откройте страницу с параметром <code>?key=…</code>.
        </p>
      )}
    </main>
  );
}
