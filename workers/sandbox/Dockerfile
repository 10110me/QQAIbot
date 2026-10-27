FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends docker.io docker-cli && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi 'uvicorn[standard]'
WORKDIR /app
COPY workers/resource_slot.py /app/resource_slot.py
COPY workers/sandbox/service.py ./service.py
COPY workers/sandbox/project_runner.py ./project_runner.py
COPY workers/sandbox/python_runner.py ./python_runner.py
COPY src/osa/domain /app/src/osa/domain
ENV PYTHONPATH=/app/src
EXPOSE 8091
CMD ["python", "-m", "uvicorn", "service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--no-access-log"]
