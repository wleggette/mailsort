FROM python:3.12-slim

WORKDIR /app

# Copy package source and metadata — src/ must be present before pip install
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config.yaml ./config.yaml
RUN mkdir -p /app/data

EXPOSE 8025 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8025/health')" || exit 1

CMD ["mailsort", "start"]
