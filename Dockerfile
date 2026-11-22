FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# kubectl (the Kubernetes tool shells out to `kubectl logs`). Pinned + checksum-verified.
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.31.0
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && curl -fsSLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl" \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${ARCH}/kubectl.sha256" -o kubectl.sha256 \
    && echo "$(cat kubectl.sha256)  kubectl" | sha256sum -c - \
    && install -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl kubectl.sha256 \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI: EKS kubeconfigs authenticate via `aws eks get-token`. Isolated venv so its
# botocore pins can't conflict with boto3 in requirements.txt.
ARG AWSCLI_VERSION=1.46.3
RUN python -m venv /opt/awscli \
    && /opt/awscli/bin/pip install --no-cache-dir "awscli==${AWSCLI_VERSION}" \
    && ln -s /opt/awscli/bin/aws /usr/local/bin/aws

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
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
