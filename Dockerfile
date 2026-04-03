FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached unless pyproject.toml changes)
COPY pyproject.toml README.md ./
RUN mkdir -p src/mailsort && \
    touch src/mailsort/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf src/mailsort

# Copy actual source (only this layer rebuilds on code changes)
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps .

COPY config.yaml ./config.yaml
RUN mkdir -p /app/data

EXPOSE 8025 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8025/health')" || exit 1

CMD ["mailsort", "start"]
