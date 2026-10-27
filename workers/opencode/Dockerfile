FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io docker-cli && rm -rf /var/lib/apt/lists/*
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
RUN pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn[standard]>=0.30,<1' 'httpx>=0.28,<1'
WORKDIR /app
COPY workers/opencode/service.py ./service.py
EXPOSE 8094
CMD ["python", "-m", "uvicorn", "service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8094", "--no-access-log"]
