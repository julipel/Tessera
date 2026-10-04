// Все варианты UI-компонентов для демо-страницы (/demo/components) и скриншот-тестов.
// Картинки локальные (public/demo) — скриншоты детерминированы и не зависят от сети.
import type { Component, MessageBlock, ProductCard } from "@/contracts";

const cream: ProductCard = {
  type: "product_card",
  entity_id: "e_cream",
  title: "Увлажняющий крем для лица с гиалуроновой кислотой",
  subtitle: "Уход за лицом",
  image_url: "/demo/cream.svg",
  price: { amount: 1990, currency: "RUB" },
  badges: ["В наличии", "Хит"],
  url: "https://example.com/cream",
  actions: [
    { action_id: "select_product", label: "Выбрать", style: "primary", payload: { entity_id: "e_cream" } },
    { action_id: "open", label: "На сайте", style: "link", url: "https://example.com/cream" },
  ],
};

const serum: ProductCard = {
  type: "product_card",
  entity_id: "e_serum",
  title: "Сыворотка с витамином C",
  subtitle: "Уход за лицом",
  image_url: "/demo/serum.svg",
  price: { amount: 2490.5, currency: "RUB" },
  badges: ["Под заказ"],
  url: null,
  actions: [],
};

/** Без цены и без картинки: услуга, «цена по запросу». */
const consultation: ProductCard = {
  type: "product_card",
  entity_id: "e_consult",
  title: "Консультация косметолога",
  subtitle: "Услуги",
  image_url: null,
  price: null,
  badges: [],
  url: "https://example.com/consult",
};

const mask: ProductCard = {
  type: "product_card",
  entity_id: "e_mask",
  title: "Маска для лица",
  image_url: "/demo/mask.svg",
  price: { amount: 49, currency: "SEK" },
};

export const DEMO_SECTIONS: { id: string; title: string; components: Component[] }[] = [
  { id: "product-card", title: "product_card", components: [cream, consultation] },
  {
    id: "product-carousel",
    title: "product_carousel",
    components: [{ type: "product_carousel", title: "Подходящие варианты", items: [cream, serum, mask, consultation] }],
  },
  {
    id: "comparison-table",
    title: "comparison_table",
    components: [
      {
        type: "comparison_table",
        columns: ["Увлажняющий крем", "Сыворотка с витамином C", "Маска для лица"],
        rows: [
          { label: "Цена", values: ["1990 RUB", "2490.5 RUB", "49 SEK"] },
          { label: "Наличие", values: ["В наличии", "Под заказ", ""] },
          { label: "Тип кожи", values: ["Сухая, нормальная", "Любой", "Жирная"] },
        ],
      },
    ],
  },
  {
    id: "info-card",
    title: "info_card",
    components: [
      {
        type: "info_card",
        title: "Доставка и оплата",
        body_markdown:
          "Доставляем **по всей России** за 2–5 дней.\n\n- Курьером — 300 ₽\n- Самовывоз — бесплатно\n\nПодробнее — [на сайте](https://example.com/delivery).",
        image_url: null,
        url: "https://example.com/delivery",
      },
    ],
  },
  {
    id: "image",
    title: "image",
    components: [{ type: "image", url: "/demo/serum.svg", alt: "Сыворотка с витамином C", caption: "Флакон 30 мл" }],
  },
  {
    id: "link-list",
    title: "link_list",
    components: [
      {
        type: "link_list",
        items: [
          { title: "Как выбрать крем", url: "https://example.com/guide", description: "Гид по типам кожи" },
          { title: "Возврат товара", url: "https://example.com/returns" },
        ],
      },
    ],
  },
  {
    id: "sources",
    title: "sources",
    components: [
      {
        type: "sources",
        items: [
          {
            title: "Доставка — FAQ",
            url: "https://example.com/faq#delivery",
            snippet: "Заказы от 3000 ₽ доставляем бесплатно. Срок доставки зависит от региона и составляет от двух до пяти рабочих дней.",
          },
          { title: "Политика возврата", url: "https://example.com/returns" },
        ],
      },
    ],
  },
  {
    id: "form",
    title: "form",
    components: [
      {
        type: "form",
        form_id: "f_demo",
        title: "Оставьте контакт",
        fields: [
          { name: "name", label: "Имя", kind: "text", required: true },
          { name: "phone", label: "Телефон", kind: "phone", required: true },
          { name: "email", label: "Email", kind: "email" },
          {
            name: "service",
            label: "Услуга",
            kind: "select",
            options: [
              { label: "Консультация", value: "consult" },
              { label: "Чистка лица", value: "cleaning" },
            ],
          },
          { name: "date", label: "Удобная дата", kind: "date" },
          { name: "comment", label: "Комментарий", kind: "textarea" },
        ],
        submit_label: "Отправить заявку",
      },
    ],
  },
  {
    id: "confirm",
    title: "confirm",
    components: [
      {
        type: "confirm",
        confirm_id: "cf_demo",
        text: "Создать заявку на консультацию косметолога?",
        confirm_action: { action_id: "confirm", label: "Да, создать" },
        cancel_action: { action_id: "cancel", label: "Отмена", style: "secondary" },
      },
    ],
  },
];

/** Сообщение ассистента: markdown-текст, затем карточка — как в чате после show_entities. */
export const DEMO_MESSAGE: MessageBlock[] = [
  {
    type: "text",
    block_id: "m-b1",
    text: "Для **сухой кожи** подойдут:\n\n1. Увлажняющий крем — на каждый день\n2. Сыворотка — курсом\n\n<b>сырой HTML не выводится</b> ![и картинки из текста](/demo/mask.svg)",
  },
  { type: "component", block_id: "m-b2", component: serum },
];
