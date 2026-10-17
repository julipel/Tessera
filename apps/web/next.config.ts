import path from "node:path";
import type { NextConfig } from "next";

// Встраивать во фрейм можно только `/widget` — его frame-ancestors по ключу ставит proxy.ts
// (ADR-0022). Остальное (полноэкранный чат `/`, витрины, админка) — нигде: иначе чужой сайт
// показал бы чат тенанта с его ключом в обход allowed_origins (REVIEW.md: W-1). Заголовок
// из proxy перезаписывает этот (у админки — своя CSP с тем же frame-ancestors); `/widget`
// исключён, чтобы не зависеть от порядка их применения.
const NO_FRAMING = [{ key: "Content-Security-Policy", value: "frame-ancestors 'none'" }];

const nextConfig: NextConfig = {
  // Образ web (apps/web/Dockerfile, ADR-0040): server.js и только нужные node_modules.
  // Корень трассировки — монорепо: зависимости pnpm лежат в node_modules корня.
  output: "standalone",
  outputFileTracingRoot: path.join(import.meta.dirname, "../.."),
  poweredByHeader: false,
  async headers() {
    return [
      { source: "/:path((?!widget$).*)", headers: NO_FRAMING },
    ];
  },
};

export default nextConfig;
