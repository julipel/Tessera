import { notFound } from "next/navigation";
import { MessageBlocks } from "@/components/chat/MessageBlocks";
import { DEMO_MESSAGE, DEMO_SECTIONS } from "@/components/rich/fixtures";

// Витрина всех UI-компонентов чата — для разработки и скриншот-тестов (e2e/components.spec.ts).
export default function ComponentsDemo() {
  if (process.env.NODE_ENV === "production") notFound();

  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-8 px-4 py-6">
      <h1 className="text-xl font-semibold">UI-компоненты чата</h1>
      <section id="message" data-testid="message" className="-mx-4 flex flex-col gap-2 px-4">
        <h2 className="font-mono text-sm text-chat-muted">текст (markdown) + компонент</h2>
        <MessageBlocks blocks={DEMO_MESSAGE} />
      </section>
      {DEMO_SECTIONS.map((s) => (
        <section key={s.id} id={s.id} data-testid={s.id} className="-mx-4 flex flex-col gap-2 px-4">
          <h2 className="font-mono text-sm text-chat-muted">{s.title}</h2>
          <MessageBlocks
            blocks={s.components.map((component, i) => ({ type: "component", block_id: `${s.id}-${i}`, component }))}
          />
        </section>
      ))}
    </main>
  );
}
