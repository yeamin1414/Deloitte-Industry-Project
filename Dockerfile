# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAP_DATA_DIR=/app/data
RUN groupadd --system kap && useradd --system --gid kap --home /app kap
WORKDIR /app
COPY --from=build /install /usr/local
COPY app ./app
COPY config ./config
COPY simulator ./simulator
RUN mkdir -p /app/data && chown -R kap:kap /app
USER kap
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
