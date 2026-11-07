FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m pip install "uv==0.11.24"
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv export --frozen --no-dev --extra s3 --no-emit-project --output-file /tmp/requirements.txt \
    && python -m pip wheel --wheel-dir /wheels -r /tmp/requirements.txt
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GITHUB_BACKUP_DESTINATION=/backups \
    DEBIAN_FRONTEND=noninteractive

RUN mkdir /cache && chown 10001:10001 /cache
RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git git-lfs openssl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ghbackup \
    && useradd --uid 10001 --gid ghbackup --create-home ghbackup

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* \
    && python -m pip uninstall --yes pip \
    && rm -rf /wheels

USER ghbackup
WORKDIR /home/ghbackup
ENTRYPOINT ["github-backup"]
CMD ["run"]
