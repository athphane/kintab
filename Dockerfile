FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ bot/
COPY web/ web/
COPY main.py .
RUN mkdir -p data workdir && chown -R 1000:1000 data workdir
ENV PYTHONUNBUFFERED=1 KINTAB_WEB_HOST=0.0.0.0
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"
CMD ["python", "main.py"]
