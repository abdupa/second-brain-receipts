# Runtime image for the Telegram receipt webhook service.
#
# Build:  docker build -t second-brain-receipts .
# Run:    docker run --rm -p 8000:8000 --env-file .env second-brain-receipts
#
# No credentials are baked into the image: every setting is read from the
# environment at start-up (see .env.example and README.md). Do not COPY a .env
# file into the image or pass secrets as build args, which persist in layers.

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Direct runtime dependencies are pinned exactly in pyproject.toml. The dev lock
# snapshot (requirements-dev.lock) additionally pins transitive versions but also
# carries pytest/ruff/mypy, which have no place in a runtime image.
COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --upgrade pip \
    && /opt/venv/bin/python -m pip install .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged runtime user; the service writes no files and needs no shell.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

USER appuser
WORKDIR /home/appuser

EXPOSE 8000

# GET /health returns {"status":"ok"} and probes no provider, so it stays cheap
# and never depends on Telegram, OpenAI, Supabase or S3 being reachable.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# The application factory loads settings explicitly; import performs no I/O.
CMD ["uvicorn", "second_brain_receipts.main:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
