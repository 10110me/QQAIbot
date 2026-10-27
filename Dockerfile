FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts/backup.py /app/scripts/backup.py
RUN useradd --uid 10001 --create-home osa && mkdir -p /app/data /app/control /app/workspaces && chown -R osa:osa /app
USER osa
EXPOSE 8080
CMD ["sh", "-c", "alembic upgrade head && osa"]
