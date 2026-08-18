FROM python:3.12-slim

ARG TERMROOM_VERSION=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

LABEL org.opencontainers.image.source="https://github.com/huhumanmaninganingansalamlam/termroom" \
      org.opencontainers.image.title="Termroom" \
      org.opencontainers.image.description="Persistent browser workspace for local and SSH Linux terminals and files"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu neovim openssh-client tmux \
    && rm -rf /var/lib/apt/lists/*

COPY docker/termroom-entrypoint.sh /usr/local/bin/termroom-entrypoint
RUN if [ -n "$TERMROOM_VERSION" ]; then \
      pip install --no-cache-dir "termroom==${TERMROOM_VERSION}"; \
    else \
      pip install --no-cache-dir termroom; \
    fi \
    && groupadd --gid 1000 termroom \
    && useradd --create-home --uid 1000 --gid 1000 termroom \
    && mkdir -p /config /workspaces \
    && chmod 700 /config \
    && chown -R termroom:termroom /config /workspaces \
    && chmod 755 /usr/local/bin/termroom-entrypoint

WORKDIR /workspaces

EXPOSE 8765
VOLUME ["/config", "/workspaces"]

ENTRYPOINT ["/usr/local/bin/termroom-entrypoint"]
CMD ["/workspaces", "--foreground", "--host", "0.0.0.0", "--port", "8765", "--config-dir", "/config", "--no-open"]
