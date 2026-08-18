FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY module ./module
COPY settings ./settings

RUN useradd --create-home bot \
    && mkdir -p /app/settings /app/runtime/data /app/runtime/backups \
    && chown -R bot:bot /app

USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys; sys.exit(0 if b'module.main' in open('/proc/1/cmdline', 'rb').read() else 1)"]

CMD ["python", "-m", "module.main"]
