FROM python:3.14-slim

# uv ставим из официального образа — быстрый и без лишних слоёв.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Сначала только манифесты — кешируем установку зависимостей.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Затем исходники.
COPY . .
RUN uv sync --frozen --no-dev

# БД лежит в volume, чтобы переживать перезапуски.
ENV DB_PATH=/data/bridge.db
VOLUME ["/data"]

CMD ["uv", "run", "--no-dev", "python", "main.py"]
