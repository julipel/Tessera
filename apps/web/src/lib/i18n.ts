"use client";

// Строки интерфейса чата на языке диалога (ADR-0025): язык — `PublicAssistant.language`
// (бэкенд выбирает его по `locale` клиента и конфигу тенанта). Тексты тенанта (приветствие,
// подсказки, кнопки компонентов) приходят с бэкенда уже на этом языке — здесь только
// подписи платформы.
import { createContext, useContext } from "react";
import type { PublicAssistant } from "@/contracts";

export type Language = PublicAssistant["language"];

export const DEFAULT_LANGUAGE: Language = "ru";

export interface Messages {
  assistantName: string;
  typing: string;
  messageLabel: string;
  messagePlaceholder: string;
  send: string;
  stop: string;
  collapse: string;
  messagesLabel: string;
  interrupted: string;
  failed: string;
  retry: string;
  toNewMessages: string;
  suggestionsLabel: string;
  selectPlaceholder: string;
  formSubmit: string;
  formSent: string;
  confirmLabel: string;
  sources: string;
  carousel: string;
  networkError: string;
}

export const MESSAGES: Record<Language, Messages> = {
  ru: {
    assistantName: "AI-консультант",
    typing: "Печатает…",
    messageLabel: "Сообщение",
    messagePlaceholder: "Напишите сообщение…",
    send: "Отправить",
    stop: "Остановить",
    collapse: "Свернуть",
    messagesLabel: "Сообщения",
    interrupted: "Ответ прерван",
    failed: "Не удалось ответить",
    retry: "Повторить",
    toNewMessages: "К новым сообщениям",
    suggestionsLabel: "Быстрые ответы",
    selectPlaceholder: "Выберите…",
    formSubmit: "Отправить",
    formSent: "Форма отправлена",
    confirmLabel: "Подтверждение",
    sources: "Источники",
    carousel: "Подборка",
    networkError: "Нет связи с сервером. Попробуйте ещё раз.",
  },
  en: {
    assistantName: "AI assistant",
    typing: "Typing…",
    messageLabel: "Message",
    messagePlaceholder: "Type a message…",
    send: "Send",
    stop: "Stop",
    collapse: "Minimize",
    messagesLabel: "Messages",
    interrupted: "Answer interrupted",
    failed: "Couldn't answer",
    retry: "Retry",
    toNewMessages: "To new messages",
    suggestionsLabel: "Quick replies",
    selectPlaceholder: "Choose…",
    formSubmit: "Submit",
    formSent: "Form submitted",
    confirmLabel: "Confirmation",
    sources: "Sources",
    carousel: "Selection",
    networkError: "No connection to the server. Please try again.",
  },
  sv: {
    assistantName: "AI-assistent",
    typing: "Skriver…",
    messageLabel: "Meddelande",
    messagePlaceholder: "Skriv ett meddelande…",
    send: "Skicka",
    stop: "Stoppa",
    collapse: "Minimera",
    messagesLabel: "Meddelanden",
    interrupted: "Svaret avbröts",
    failed: "Kunde inte svara",
    retry: "Försök igen",
    toNewMessages: "Till nya meddelanden",
    suggestionsLabel: "Snabbsvar",
    selectPlaceholder: "Välj…",
    formSubmit: "Skicka",
    formSent: "Formuläret skickat",
    confirmLabel: "Bekräftelse",
    sources: "Källor",
    carousel: "Urval",
    networkError: "Ingen anslutning till servern. Försök igen.",
  },
};

/** Локаль `Intl` (цены) по языку диалога. */
export const LOCALES: Record<Language, string> = { ru: "ru-RU", en: "en-US", sv: "sv-SE" };

export interface I18n {
  language: Language;
  t: Messages;
  locale: string;
}

export function i18n(language: Language | undefined): I18n {
  const lang = language && language in MESSAGES ? language : DEFAULT_LANGUAGE;
  return { language: lang, t: MESSAGES[lang], locale: LOCALES[lang] };
}

/** Язык браузера для `locale` в API (BCP 47); на сервере и без navigator — undefined. */
export function browserLocale(): string | undefined {
  if (typeof navigator === "undefined") return undefined;
  return navigator.language || undefined;
}

// Без провайдера (демо компонентов) — русский, как до P6-04.
const I18nContext = createContext<I18n>(i18n(DEFAULT_LANGUAGE));

export const I18nProvider = I18nContext.Provider;

export function useI18n(): I18n {
  return useContext(I18nContext);
}
