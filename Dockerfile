FROM python:3.12-slim

# Unprivileged account the entrypoint drops to. Deliberately NO USER directive
# in this file: docker-entrypoint.sh needs root once at start to chown the
# bind-mounted /app/data (Docker creates a missing ./data as root:root, and
# older images wrote root-owned files), then re-execs server.py as uid 1000 so
# the server itself never runs as root. For an unprivileged shell:
# `docker exec -u chore-chart <container> sh`.
RUN groupadd -g 1000 chore-chart && \
    useradd -u 1000 -g chore-chart -d /home/chore-chart -m --no-log-init chore-chart && \
    mkdir -p /app/data && \
    chown -R chore-chart:chore-chart /app/data

WORKDIR /app

# Application files owned by root:root and read-only to chore-chart at runtime
COPY --chown=root:root app/server.py .
COPY --chown=root:root --chmod=755 app/docker-entrypoint.sh .
COPY --chown=root:root html/ ./html/
COPY --chown=root:root LICENSE ./LICENSE

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --retries=3 --start-period=5s \
  CMD python3 -c "import os,urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/state', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "server.py"]
