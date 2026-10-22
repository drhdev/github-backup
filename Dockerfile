FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m pip install "uv==0.11.24"
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv export --frozen --no-dev --extra s3 --no-emit-project --output-file /tmp/requirements.txt \
    && python -m pip wheel --wheel-dir /wheels -r /tmp/requirements.txt
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.13-slim-bookworm@sha256:a1165e272e578941b84abc79e4ab38a0305cd12803a5c4247979ac7655f4d641

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GITHUB_BACKUP_DESTINATION=/backups \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git git-lfs \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ghbackup \
    && useradd --uid 10001 --gid ghbackup --create-home ghbackup

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels

USER ghbackup
WORKDIR /home/ghbackup
ENTRYPOINT ["github-backup"]
CMD ["run"]
