# Образ web (P9-01, ADR-0040): Next.js standalone-сервер.
# Сборка из корня репозитория:
#   docker build -f apps/web/Dockerfile --build-arg NEXT_PUBLIC_API_URL=https://<домен> .
# NEXT_PUBLIC_API_URL вшивается в сборку — смена адреса API требует пересборки.
FROM node:24-slim AS build

ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable

WORKDIR /repo
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY packages/contracts/package.json packages/contracts/
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile --filter web

COPY apps/web apps/web
ARG NEXT_PUBLIC_API_URL
RUN test -n "$NEXT_PUBLIC_API_URL" || { echo "нужен --build-arg NEXT_PUBLIC_API_URL" >&2; exit 1; }
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
RUN pnpm --filter web exec next build

FROM node:24-slim

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000
WORKDIR /srv
# Standalone повторяет раскладку монорепо от outputFileTracingRoot (next.config.ts);
# public и .next/static он не копирует — раздаёт их тот же server.js.
COPY --from=build --chown=node /repo/apps/web/.next/standalone ./
COPY --from=build --chown=node /repo/apps/web/.next/static ./apps/web/.next/static
COPY --from=build --chown=node /repo/apps/web/public ./apps/web/public
USER node

EXPOSE 3000
CMD ["node", "apps/web/server.js"]
