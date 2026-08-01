FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 vislex \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin vislex

ARG VISLEX_VERSION=1.1.2
ARG VISLEX_REVISION=unknown

LABEL org.opencontainers.image.title="Vislex" \
      org.opencontainers.image.description="Local video understanding, transcription, and Markdown archive" \
      org.opencontainers.image.source="https://github.com/shaundcn/vislex" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VISLEX_VERSION}" \
      org.opencontainers.image.revision="${VISLEX_REVISION}"

COPY LICENSE /usr/share/licenses/vislex/LICENSE
COPY --chown=10001:10001 app ./app
RUN mkdir -p /app/input /app/output /app/data \
    && chown -R 10001:10001 /app/input /app/output /app/data

ENV UVICORN_PORT=9602

EXPOSE 9602

ENTRYPOINT ["python", "-m", "app.entrypoint"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--workers", "1", "--no-access-log"]
