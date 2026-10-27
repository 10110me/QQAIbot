"""Document parsing is an isolated bounded subprocess, not the credentialed app."""
import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from resource_slot import HeavySlot


class ParseInput(BaseModel):
    filename: str = Field(max_length=300)
    content: str = Field(max_length=28_000_000)


class VideoInput(BaseModel):
    content: str = Field(max_length=28_000_000)


def parse_child() -> None:
    import resource
    memory = int(os.environ.get("PARSER_MEMORY_MB", "768")) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024*1024, 1024*1024))
    from osa.infrastructure.features.parsers import parse_document
    data = json.load(sys.stdin)
    raw = base64.b64decode(data["content"], validate=True)
    if len(raw) > 20_000_000:
        raise ValueError("File too large")
    result = parse_document(raw, data["filename"])
    if sum(len(page["text"]) for page in result) > 2_000_000:
        raise ValueError("Parsed content exceeds limit")
    json.dump(result, sys.stdout, ensure_ascii=False)


def create_app() -> FastAPI:
    token = os.environ.get("OSA_WORKER_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("Worker token not configured")
    lock = asyncio.Lock()

    async def auth(authorization: str = Header(default="")) -> None:
        if not secrets.compare_digest(authorization, "Bearer " + token):
            raise HTTPException(401, "Unauthorized")

    app = FastAPI(dependencies=[Depends(auth)])

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ready"}

    @app.post("/parse")
    async def parse(data: ParseInput) -> list:
        async with lock, HeavySlot():
            # Child receives no app credentials or worker token.
            process = await asyncio.create_subprocess_exec(sys.executable, __file__, "--child",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "/app/src",
                     "PARSER_MEMORY_MB": os.environ.get("PARSER_MEMORY_MB", "768")}, start_new_session=True)
            try:
                output, _ = await asyncio.wait_for(process.communicate(data.model_dump_json().encode()), 25)
                if process.returncode or len(output) > 8_000_000:
                    raise HTTPException(422, "Document parsing rejected")
                return json.loads(output)
            except (TimeoutError, asyncio.CancelledError):
                process.kill()
                await process.wait()
                raise

    @app.post("/video/frames")
    async def video_frames(data: VideoInput) -> dict:
        async with lock, HeavySlot():
            raw = base64.b64decode(data.content, validate=True)
            if len(raw) > 20_000_000:
                raise HTTPException(413, "Video too large")
            with tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "video.bin"
                source.write_bytes(raw)
                process = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-nostdin",
                    "-threads", "1", "-filter_threads", "1",
                    "-protocol_whitelist", "file,pipe", "-i", str(source), "-t", "60", "-vf", "fps=1/10,scale=768:-1",
                    "-frames:v", "6", "-threads", "1", str(Path(temporary) / "frame-%02d.jpg"),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    env={"PATH": os.environ.get("PATH", "")}, start_new_session=True)
                try:
                    await asyncio.wait_for(process.wait(), 30)
                except (TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode:
                    raise HTTPException(422, "Video decoding failed")
                frames = [base64.b64encode(file.read_bytes()).decode() for file in sorted(Path(temporary).glob("frame-*.jpg"))]
                return {"frames": frames}

    @app.post("/video/audio")
    async def video_audio(data: VideoInput) -> dict:
        async with lock, HeavySlot():
            raw = base64.b64decode(data.content, validate=True)
            if len(raw) > 20_000_000:
                raise HTTPException(413, "Video too large")
            with tempfile.TemporaryDirectory() as temporary:
                source, target = Path(temporary)/"video.bin", Path(temporary)/"audio.wav"
                source.write_bytes(raw)
                process = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-nostdin",
                    "-threads", "1", "-protocol_whitelist", "file,pipe", "-i", str(source),
                    "-t", "60", "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", "16000",
                    "-fs", "2000000", "-y", str(target), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, env={"PATH": os.environ.get("PATH", "")}, start_new_session=True)
                try:
                    await asyncio.wait_for(process.wait(), 30)
                except (TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode or not target.exists() or target.stat().st_size < 100:
                    return {"audio": None, "status": "no_audio_or_decode_failed", "seconds_limit": 60}
                if target.stat().st_size > 2_000_000:
                    raise HTTPException(413, "Decoded audio too large")
                return {"audio": base64.b64encode(target.read_bytes()).decode(), "mime": "audio/wav", "seconds_limit": 60}

    return app


if __name__ == "__main__" and "--child" in sys.argv:
    parse_child()
