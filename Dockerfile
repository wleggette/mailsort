FROM python:3.12-slim

WORKDIR /app

# Copy package source and metadata — src/ must be present before pip install
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

COPY config.yaml ./config.yaml
RUN mkdir -p /app/data

CMD ["mailsort", "start"]
