import base64
import httpx
from osa.domain.features import ParserRejected


class ParserWorkerClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    async def parse(self, content: bytes, filename: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=240) as client:
            response = await client.post(self.url + "/parse", headers={"Authorization": "Bearer " + self.token},
                json={"filename": filename, "content": base64.b64encode(content).decode()})
            if response.status_code == 422:
                raise ParserRejected("Document parsing rejected")
            response.raise_for_status()
            return response.json()
