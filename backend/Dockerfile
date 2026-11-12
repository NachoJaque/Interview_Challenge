FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY entrypoint.sh .

# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 appuser && chmod +x entrypoint.sh
USER appuser

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
