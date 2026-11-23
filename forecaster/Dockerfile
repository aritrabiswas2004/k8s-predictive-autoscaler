# Pinned by digest (multi-arch index); Dependabot proposes updates.
FROM python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

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
