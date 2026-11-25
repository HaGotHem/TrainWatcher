FROM python:3.12-slim

ENV TZ=Europe/Paris \
    PYTHONUNBUFFERED=1

RUN useradd --uid 1000 --create-home app

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chown app:app /app/data

USER app

CMD ["uvicorn", "monatrain.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
