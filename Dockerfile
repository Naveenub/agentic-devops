FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached unless requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Runtime code + knowledge base (retrieval.py reads data/ relative to the repo root).
COPY agent/ agent/
COPY api/ api/
COPY data/ data/

# Non-root user; /data is where the optional Chroma store lives.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/chroma \
    && chown -R app:app /app /data
USER app

ENV CHROMA_PATH=/data/chroma

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/docs', timeout=3)" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
