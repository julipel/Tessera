import type { Component } from "@/contracts";
import type { OnAction } from "./ActionButton";
import { ComparisonTable } from "./ComparisonTable";
import { ImageBlock, InfoCard, LinkList, Sources } from "./Content";
import { ConfirmView, FormView, type OnSubmitForm } from "./Forms";
import { ProductCard } from "./ProductCard";
import { ProductCarousel } from "./ProductCarousel";

/** UI-компонент из события `component` / истории. Неизвестный type (новее клиента) не рендерится. */
export function ComponentView({
  component,
  onAction,
  onSubmitForm,
}: {
  component: Component;
  onAction?: OnAction;
  onSubmitForm?: OnSubmitForm;
}) {
  switch (component.type) {
    case "product_card":
      return <ProductCard card={component} onAction={onAction} />;
    case "product_carousel":
      return <ProductCarousel data={component} onAction={onAction} />;
    case "comparison_table":
      return <ComparisonTable data={component} />;
    case "info_card":
      return <InfoCard data={component} />;
    case "image":
      return <ImageBlock data={component} />;
    case "link_list":
      return <LinkList data={component} />;
    case "sources":
      return <Sources data={component} />;
    case "form":
      return <FormView data={component} onSubmit={onSubmitForm} />;
    case "confirm":
      return <ConfirmView data={component} onAction={onAction} />;
    default:
      return null;
  }
}
