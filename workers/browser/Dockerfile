FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
USER root
RUN apt-get update && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify supervisor fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi 'uvicorn[standard]' 'playwright==1.58.0'
WORKDIR /app
COPY workers/resource_slot.py /app/resource_slot.py
COPY workers/browser/service.py ./service.py
COPY workers/browser/start_browser.py ./start_browser.py
COPY workers/browser/supervisord.conf /app/supervisord.conf
RUN mkdir -p /data && chown -R pwuser:pwuser /app /data
USER pwuser
ENV DISPLAY=:99 BROWSER_PROFILE=/data/profile
EXPOSE 8090 6080
CMD ["python", "/app/start_browser.py"]
