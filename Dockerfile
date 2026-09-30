# One image, three roles (migrate / web / worker) selected by the compose `command`.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so code edits don't bust the pip layer cache.
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /data/attachments && chown -R app:app /data

COPY --chown=app:app alembic.ini ./
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app app ./app

USER app
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
