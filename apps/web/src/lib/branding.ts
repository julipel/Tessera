import type { CSSProperties } from "react";
import type { BrandingConfig } from "@/contracts";

// Те же форматы, что в BrandingTokens (agent_config.schema.json): API уже проверил конфиг,
// здесь — вторая линия, чтобы в CSS-переменную не попало ничего, кроме значения токена.
const HEX_COLOR = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i;
const LENGTH = /^(0|[0-9]+(\.[0-9]+)?(px|rem|em))$/;
const FONT_FAMILY = /^[A-Za-z0-9][A-Za-z0-9 -]{0,63}$/;
const LOGO_URL = /^(https?:\/\/|\/)[^\s"'<>()\\]*$/;

// Запасной стек, если шрифта тенанта нет у посетителя (веб-шрифты не загружаются).
const FALLBACK_FONTS = "ui-sans-serif, system-ui, sans-serif";

/**
 * CSS-переменные темы чата (globals.css) из токенов тенанта. Невалидные и отсутствующие
 * токены пропускаются — остаётся нейтральная тема.
 */
export function brandingStyle(branding: BrandingConfig | undefined): CSSProperties {
  const tokens = branding?.tokens ?? {};
  const vars: Record<string, string> = {};
  if (tokens.primary && HEX_COLOR.test(tokens.primary)) {
    vars["--chat-primary"] = tokens.primary;
    vars["--chat-on-primary"] = isLight(tokens.primary) ? "#1f2328" : "#ffffff";
  }
  if (tokens.radius && LENGTH.test(tokens.radius)) vars["--chat-radius"] = tokens.radius;
  if (tokens.font && FONT_FAMILY.test(tokens.font)) {
    vars["--chat-font"] = `"${tokens.font}", ${FALLBACK_FONTS}`;
  }
  return vars as CSSProperties;
}

/** URL логотипа, если он в допустимом формате. */
export function logoUrl(branding: BrandingConfig | undefined): string | null {
  const url = branding?.logo_url;
  return url && LOGO_URL.test(url) ? url : null;
}

/** Светлый ли цвет: на нём тёмный текст читается лучше белого (относительная яркость WCAG). */
function isLight(hex: string): boolean {
  const digits = hex.length === 4 ? [...hex.slice(1)].map((d) => d + d) : hex.slice(1).match(/../g)!;
  const [r, g, b] = digits.map((d) => {
    const c = parseInt(d, 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  // Порог, при котором контраст с чёрным и белым одинаков: (L + 0.05)² = 1.05 · 0.05.
  return luminance > 0.179;
}
