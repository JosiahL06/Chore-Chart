FROM python:3.12-slim

WORKDIR /app
COPY app/server.py .
COPY html/ ./html/
COPY LICENSE ./LICENSE

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --retries=3 --start-period=5s \
  CMD python3 -c "import os,urllib.request as u,sys; sys.exit(0 if u.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/state', timeout=3).status == 200 else 1)"

CMD ["python3", "server.py"]