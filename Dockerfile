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

COPY --chown=10001:10001 app ./app
RUN mkdir -p /app/input /app/output /app/data \
    && chown -R 10001:10001 /app/input /app/output /app/data

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
