FROM ghcr.io/astral-sh/uv:0.11.14@sha256:1025398289b62de8269e70c45b91ffa37c373f38118d7da036fb8bb8efc85d97 AS uv
FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151 AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /opt/app
COPY pyproject.toml uv.lock README.md LICENSE THIRD_PARTY_NOTICES.md TRADEMARKS.md ./
COPY src ./src
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --extra hosted --no-dev --no-editable --python /usr/local/bin/python

FROM python:3.14-slim-trixie@sha256:c3e521df8b2b498a7a682e7e18676771cb80c6b75b8699af886b2d554ce40151
LABEL org.opencontainers.image.source="https://github.com/pubship/pubship" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin mcp \
    && install -d -m 0700 -o 10001 -g 10001 /var/lib/pubship-transfers
COPY --from=build /opt/app/.venv /opt/app/.venv
ENV PATH="/opt/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /home/mcp
EXPOSE 8080
ENTRYPOINT ["pubship-server"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
