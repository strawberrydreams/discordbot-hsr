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

CMD ["python", "-m", "module.main"]
