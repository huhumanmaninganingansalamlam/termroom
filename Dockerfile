FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu openssh-client tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY termroom /app/termroom
COPY docker/termroom-entrypoint.sh /usr/local/bin/termroom-entrypoint
RUN pip install --no-cache-dir . \
    && groupadd --gid 1000 termroom \
    && useradd --create-home --uid 1000 --gid 1000 termroom \
    && mkdir -p /config /workspaces \
    && chmod 700 /config \
    && chown -R termroom:termroom /config /workspaces \
    && chmod 755 /usr/local/bin/termroom-entrypoint

EXPOSE 8765
VOLUME ["/config", "/workspaces"]

ENTRYPOINT ["/usr/local/bin/termroom-entrypoint"]
CMD ["/workspaces", "--foreground", "--host", "0.0.0.0", "--port", "8765", "--config-dir", "/config", "--no-open"]
