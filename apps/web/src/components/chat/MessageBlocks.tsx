import type { MessageBlock, ProductCard as ProductCardData } from "@/contracts";
import type { OnAction } from "../rich/ActionButton";
import { ComponentView } from "../rich/ComponentView";
import { CardRow } from "../rich/ProductCarousel";
import { Markdown } from "./Markdown";

type Group =
  | { kind: "block"; block: MessageBlock }
  | { kind: "cards"; key: string; cards: ProductCardData[] };

/** Подряд идущие product_card (show_entities layout=cards) — одним горизонтальным рядом. */
function group(blocks: MessageBlock[]): Group[] {
  const groups: Group[] = [];
  for (const block of blocks) {
    const last = groups.at(-1);
    if (block.type === "component" && block.component.type === "product_card") {
      if (last?.kind === "cards") last.cards.push(block.component);
      else groups.push({ kind: "cards", key: block.block_id, cards: [block.component] });
    } else {
      groups.push({ kind: "block", block });
    }
  }
  return groups;
}

export function MessageBlocks({ blocks, onAction }: { blocks: MessageBlock[]; onAction?: OnAction }) {
  return group(blocks).map((g) => {
    if (g.kind === "cards") {
      return (
        <div key={g.key} data-block="component" data-component="product_card">
          <CardRow cards={g.cards} onAction={onAction} />
        </div>
      );
    }
    const { block } = g;
    if (block.type === "text") {
      return (
        <div
          key={block.block_id}
          data-block="text"
          className="max-w-[85%] self-start rounded-chat border border-chat-border bg-chat-surface px-4 py-2"
        >
          <Markdown>{block.text}</Markdown>
        </div>
      );
    }
    return (
      <div key={block.block_id} data-block="component" data-component={block.component.type}>
        <ComponentView component={block.component} onAction={onAction} />
      </div>
    );
  });
}
