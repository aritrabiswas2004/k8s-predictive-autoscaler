# Pinned by digest (multi-arch index); Dependabot proposes updates.
FROM python:3.15.0-slim@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app
# All dependencies ship wheels for linux/amd64 and linux/arm64, so no compiler is needed.
COPY requirements.txt .
RUN pip install --only-binary=:all: -r requirements.txt

COPY src/ src/

# Unprivileged user (same uid as distroless "nonroot").
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["python", "-m", "forecaster"]
