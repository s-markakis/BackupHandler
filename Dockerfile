# syntax=docker/dockerfile:1.7

# ─── Builder stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ReadMe.md LICENSE ./
COPY src/ ./src/

RUN pip install --upgrade pip build \
    && python -m build --wheel --outdir /wheels

# ─── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BACKUP_HANDLER_LOG_JSON=1

# Runtime OS dependencies:
#   - openssh-client: SSH/SFTP backups
#   - default-mysql-client: mysqldump
#   - rsync, tar: used by snapshots
#   - ca-certificates: TLS for S3/webhooks
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        openssh-client \
        default-mysql-client \
        rsync \
        tar \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid "${APP_GID}" backup \
    && useradd --system --uid "${APP_UID}" --gid "${APP_GID}" \
        --home /app --shell /usr/sbin/nologin backup

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY --chown=backup:backup config/ ./config/

RUN mkdir -p /app/Logs /app/BackupTimestamp /app/snapshots \
    && chown -R backup:backup /app

USER backup

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD backup-handler --version || exit 1

ENTRYPOINT ["backup-handler"]
CMD ["--help"]
