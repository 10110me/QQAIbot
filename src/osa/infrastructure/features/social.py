import httpx


class QZoneAdapter:
    """Explicit external adapter contract; no invented private QQ API or account bypass."""
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    async def timeline(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(self.url + "/timeline", headers={"Authorization": "Bearer " + self.token})
            response.raise_for_status()
            return response.json()["items"]

    async def execute(self, action: str, arguments: dict, request_id: str) -> dict:
        if action not in {"post", "comment", "like"}:
            raise PermissionError("Unsupported QQ Space operation")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(self.url + "/actions", headers={"Authorization": "Bearer " + self.token,
                "Idempotency-Key": request_id}, json={"action": action, "arguments": arguments, "request_id": request_id})
            response.raise_for_status()
            return response.json()
