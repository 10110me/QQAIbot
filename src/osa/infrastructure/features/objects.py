import asyncio
import re
from pathlib import Path


def validate_key(key: str) -> None:
    if not re.fullmatch(r"[a-f0-9]{32,64}", key):
        raise PermissionError("Object keys must be opaque hexadecimal identifiers")


class LocalBlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(self, key: str, content: bytes, mime: str) -> None:
        validate_key(key)
        target = self.root / key
        temporary = self.root / (key + ".tmp")
        await asyncio.to_thread(temporary.write_bytes, content)
        temporary.replace(target)
        target.chmod(0o600)

    async def get(self, key: str) -> bytes:
        validate_key(key)
        return await asyncio.to_thread((self.root / key).read_bytes)

    async def delete(self, key: str) -> None:
        validate_key(key)
        await asyncio.to_thread((self.root / key).unlink, missing_ok=True)


class S3BlobStore:
    def __init__(self, endpoint: str | None, bucket: str) -> None:
        import boto3
        self.client = boto3.client("s3", endpoint_url=endpoint)
        self.bucket = bucket

    async def put(self, key: str, content: bytes, mime: str) -> None:
        validate_key(key)
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key,
                                Body=content, ContentType=mime)

    async def get(self, key: str) -> bytes:
        validate_key(key)
        def read() -> bytes:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            try:
                return response["Body"].read()
            finally:
                response["Body"].close()
        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        validate_key(key)
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
