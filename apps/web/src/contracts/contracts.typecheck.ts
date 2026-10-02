// Проверка на этапе `tsc`: сгенерированные контракты импортируются и union сужается по `type`.
// В рантайме не используется.
import type { Component, Event, TextDeltaData, UserInput } from "@/contracts";

export function blockIdOf(event: Event): string | null {
  switch (event.type) {
    case "text_delta":
    case "text_done":
    case "component":
      return event.data.block_id;
    default:
      return null;
  }
}

export const sampleInput: UserInput = { type: "text", text: "Нужен подарок" };

export const sampleCard: Component = {
  type: "product_card",
  entity_id: "e_1",
  title: "Крем",
  price: { amount: 4990, currency: "SEK" },
};

export const sampleDelta: TextDeltaData = { block_id: "b1", delta: "При" };
