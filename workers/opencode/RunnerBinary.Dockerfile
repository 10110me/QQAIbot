# NAS fallback: official pinned standalone binary, verified before extraction.
# Uses the already available Python image when Docker Hub's Node image is unreachable.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep && rm -rf /var/lib/apt/lists/*
RUN python -c "import base64,hashlib,io,tarfile,urllib.request,pathlib; data=urllib.request.urlopen('https://registry.npmjs.org/opencode-linux-x64-baseline/-/opencode-linux-x64-baseline-1.2.27.tgz', timeout=180).read(); expected='8BWSFWBej689vDEjmuPV60ixH8BKcfE3DQYbGBi97O50jAhbI06mpkDzrRpULinUU2dKUijteS8b45fELANxjg=='; assert base64.b64encode(hashlib.sha512(data).digest()).decode()==expected, 'OpenCode package checksum mismatch'; archive=tarfile.open(fileobj=io.BytesIO(data),mode='r:gz'); binary=archive.extractfile('package/bin/opencode').read(); target=pathlib.Path('/usr/local/bin/opencode'); target.write_bytes(binary); target.chmod(0o755)" \
    && groupadd -g 10005 agent && useradd -u 10005 -g agent -m agent \
    && mkdir -p /project && chown agent:agent /project \
    && opencode --version
USER agent
ENV HOME=/home/agent XDG_CONFIG_HOME=/home/agent/.config XDG_DATA_HOME=/home/agent/.local/share
WORKDIR /project
EXPOSE 4096
CMD ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"]
