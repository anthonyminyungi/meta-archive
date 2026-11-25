FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir . && \
    groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app && \
    chown -R app:app /app

USER 10001:10001

ENV ARCHIVE_DIR=/data/archive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"]

CMD ["uvicorn", "meta_archive_viewer.app:app", "--host", "0.0.0.0", "--port", "8080"]
