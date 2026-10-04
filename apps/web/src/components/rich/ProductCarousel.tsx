import type { ProductCard as ProductCardData, ProductCarousel as ProductCarouselData } from "@/contracts";
import type { OnAction } from "./ActionButton";
import { ProductCard } from "./ProductCard";

/** Горизонтальный ряд карточек с прокруткой; выходит за поля колонки до краёв ленты. */
export function CardRow({ cards, onAction }: { cards: ProductCardData[]; onAction?: OnAction }) {
  return (
    <div className="-mx-4 flex snap-x snap-mandatory gap-3 overflow-x-auto px-4 pb-2">
      {cards.map((card) => (
        <ProductCard key={card.entity_id} card={card} onAction={onAction} />
      ))}
    </div>
  );
}

export function ProductCarousel({ data, onAction }: { data: ProductCarouselData; onAction?: OnAction }) {
  return (
    <section aria-label={data.title ?? "Подборка"} className="flex flex-col gap-2">
      {data.title && <h3 className="font-medium">{data.title}</h3>}
      <CardRow cards={data.items} onAction={onAction} />
    </section>
  );
}
