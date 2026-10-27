FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
RUN pip install --no-cache-dir fastapi 'uvicorn[standard]' aiohttp 'playwright==1.58.0'
WORKDIR /app
COPY workers/resource_slot.py /app/resource_slot.py
COPY src ./src
COPY workers/browser/screenshot.py ./screenshot.py
USER pwuser
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
EXPOSE 8093
CMD ["python", "-m", "uvicorn", "screenshot:create_app", "--factory", "--host", "0.0.0.0", "--port", "8093", "--no-access-log"]
