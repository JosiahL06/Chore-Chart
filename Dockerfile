FROM python:3.12-slim

RUN groupadd -g 1000 chore-chart && \
    useradd -u 1000 -g chore-chart -m chore-chart

RUN mkdir -p /app/data

RUN chown -R chore-chart:chore-chart /app/data

WORKDIR /app
COPY app/server.py .
COPY --chmod=755 app/docker-entrypoint.sh .
COPY html/ ./html/
COPY LICENSE ./LICENSE

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --retries=3 --start-period=5s \
  CMD python3 -c "import os,urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/state', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "server.py"]