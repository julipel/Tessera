import type { ProductCard as ProductCardData } from "@/contracts";
import { ActionRow, type OnAction } from "./ActionButton";
import { formatPrice } from "./format";

export function ProductCard({ card, onAction }: { card: ProductCardData; onAction?: OnAction }) {
  return (
    <article
      aria-label={card.title}
      className="flex w-60 shrink-0 snap-start flex-col overflow-hidden rounded-chat border border-chat-border bg-chat-surface"
    >
      <div className="aspect-square bg-chat-bg">
        {card.image_url ? (
          // Домены картинок у тенантов произвольные — next/image потребовал бы remotePatterns.
          // eslint-disable-next-line @next/next/no-img-element
          <img src={card.image_url} alt="" loading="lazy" className="size-full object-cover" />
        ) : (
          <div aria-hidden className="flex size-full items-center justify-center text-4xl text-chat-muted">
            ◇
          </div>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <div>
          <h3 className="font-medium leading-snug">
            {card.url ? (
              <a href={card.url} target="_blank" rel="noopener noreferrer" className="hover:underline">
                {card.title}
              </a>
            ) : (
              card.title
            )}
          </h3>
          {card.subtitle && <p className="text-sm text-chat-muted">{card.subtitle}</p>}
        </div>
        {!!card.badges?.length && (
          <ul className="flex flex-wrap gap-1">
            {card.badges.map((b) => (
              <li key={b} className="rounded-full bg-chat-bg px-2 py-0.5 text-xs text-chat-muted">
                {b}
              </li>
            ))}
          </ul>
        )}
        <div className="mt-auto flex flex-col gap-2">
          {card.price && <p className="text-lg font-semibold">{formatPrice(card.price)}</p>}
          <ActionRow actions={card.actions} onAction={onAction} />
        </div>
      </div>
    </article>
  );
}
