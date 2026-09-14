FROM python:3.12-slim AS builder

WORKDIR /build
# The wheel force-includes (pyproject [tool.hatch.build.targets.wheel.force-include])
# several files from the repo root: agent.yaml, docker-compose.adk-vllm.yml, the
# adk/webui/* + adk/* files (copied via `COPY adk/`), AND deploy/compose/*.yml.
# Any missing one fails the wheel build ("Forced include not found: /build/...").
COPY pyproject.toml README.md LICENSE docker-compose.adk-vllm.yml agent.yaml ./
COPY adk/ adk/
COPY deploy/ deploy/

RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim

LABEL org.opencontainers.image.title="awdk" \
      org.opencontainers.image.description="AitherOS Agent Development Kit server" \
      org.opencontainers.image.version="1.16.0" \
      org.opencontainers.image.vendor="Aitherium" \
      org.opencontainers.image.url="https://aitherium.com" \
      org.opencontainers.image.source="https://github.com/Aitherium/aither" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN groupadd --gid 1000 adk && \
    useradd --uid 1000 --gid adk --create-home adk

COPY --from=builder /install /usr/local

WORKDIR /home/adk
USER adk

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=4).raise_for_status()"

CMD ["adk-serve", "--host", "0.0.0.0", "--port", "8080"]
