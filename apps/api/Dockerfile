# Образ API (P9-01, ADR-0040): один на роли api, worker и migrate — команду задаёт compose.
# Сборка из корня репозитория: docker build -f apps/api/Dockerfile .
# Раскладка /srv/apps/api повторяет репозиторий: settings.py и cli.py ищут config/ и data/
# на 4 уровня выше пакета (корень — /srv).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/srv/apps/api/.venv \
    PATH=/srv/apps/api/.venv/bin:$PATH

WORKDIR /srv/apps/api

# Зависимости отдельным слоем: правка кода не переустанавливает их.
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY apps/api/alembic.ini ./
COPY apps/api/migrations ./migrations
COPY apps/api/src ./src
RUN uv sync --locked --no-dev

# Тенанты и данные пилота (ADR-0019) — для `app.cli seed` внутри контейнера.
COPY config /srv/config
COPY data/pilot /srv/data/pilot

# Файлы источников (KNOWLEDGE_FILES_DIR) — том, общий для api и worker.
RUN useradd --system --uid 10001 --home-dir /srv app \
    && mkdir -p /srv/data/knowledge \
    && chown app /srv/data/knowledge
USER app

EXPOSE 8000
# За Caddy (deploy/Caddyfile): IP посетителя для лимитов — из X-Forwarded-For (ADR-0030).
# Доверенные прокси — FORWARDED_ALLOW_IPS (подсеть compose, задаёт compose.prod.yml; без неё
# uvicorn доверяет только 127.0.0.1 и ::1 — тогда все посетители в одной корзине лимитов),
# не «*» (REVIEW.md: S-5).
# При остановке открытые SSE-стримы ждут не дольше 15 с, затем закрываются: иначе lifespan
# не дойдёт до отправки хвоста трейсов Langfuse до SIGKILL (stop_grace_period в
# compose.prod.yml — с запасом на неё; REVIEW.md: O-12). Без заголовка server (D-12).
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", \
     "--timeout-graceful-shutdown", "15", "--no-server-header"]
