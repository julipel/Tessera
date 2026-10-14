import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Образ web (apps/web/Dockerfile, ADR-0040): server.js и только нужные node_modules.
  // Корень трассировки — монорепо: зависимости pnpm лежат в node_modules корня.
  output: "standalone",
  outputFileTracingRoot: path.join(import.meta.dirname, "../.."),
};

export default nextConfig;
