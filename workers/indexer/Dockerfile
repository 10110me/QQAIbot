FROM python:3.12-slim
WORKDIR /app
COPY workers/resource_slot.py /app/resource_slot.py
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi 'uvicorn[standard]' pypdf python-docx
COPY src ./src
COPY workers/indexer/service.py ./service.py
RUN useradd --uid 10003 --create-home worker && chown -R worker:worker /app
USER worker
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
EXPOSE 8092
CMD ["python", "-m", "uvicorn", "service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8092", "--no-access-log"]
