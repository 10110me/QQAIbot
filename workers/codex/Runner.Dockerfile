ARG NODE_IMAGE=node:20-bookworm-slim
FROM ${NODE_IMAGE} AS codex-package
RUN npm install --global @openai/codex@0.147.0 && npm cache clean --force

FROM python:3.12-slim
COPY --from=codex-package /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-package /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && pip install --no-cache-dir 'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' \
    && mkdir /tmp/litellm-core \
    && pip download --no-deps --only-binary=:all: --index-url https://mirrors.aliyun.com/pypi/simple -d /tmp/litellm-core 'litellm-core==1.106.0' \
    && echo '1f5d6400556b2b74934df80424968275492f66998702613b88d218fed3cb4a0d  /tmp/litellm-core/litellm_core-1.106.0-cp310-abi3-manylinux_2_28_x86_64.whl' | sha256sum -c - \
    && pip install --no-cache-dir /tmp/litellm-core/litellm_core-1.106.0-cp310-abi3-manylinux_2_28_x86_64.whl \
    && rm -rf /tmp/litellm-core \
    && groupadd -g 10005 agent && useradd -u 10005 -g agent -m agent \
    && mkdir -p /project /home/agent/.codex \
    && chown -R agent:agent /project /home/agent/.codex \
    && codex --version
RUN pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple 'mcp==2.3.0'
COPY workers/codex/server.py /app/server.py
USER agent
ENV HOME=/home/agent CODEX_HOME=/home/agent/.codex PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LITELLM_LOCAL_MODEL_COST_MAP=True
WORKDIR /project
EXPOSE 4096
CMD ["python", "-m", "uvicorn", "server:create_app", "--factory", "--host", "0.0.0.0", "--port", "4096", "--no-access-log"]
