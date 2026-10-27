# Optional specialist runtime. Nothing is installed on the QQ/Social host.
ARG NODE_IMAGE=node:22-bookworm-slim
FROM ${NODE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep && rm -rf /var/lib/apt/lists/*
RUN npm install --global opencode-ai@1.2.27 && npm cache clean --force \
    && groupadd -g 10005 agent && useradd -u 10005 -g agent -m agent \
    && mkdir -p /project && chown agent:agent /project
USER agent
ENV HOME=/home/agent XDG_CONFIG_HOME=/home/agent/.config XDG_DATA_HOME=/home/agent/.local/share
WORKDIR /project
EXPOSE 4096
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
