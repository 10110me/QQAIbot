FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home osa && mkdir -p /app/control /app/workspaces && chown -R osa:osa /app
USER osa
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "osa.infrastructure.v2.worker"]
