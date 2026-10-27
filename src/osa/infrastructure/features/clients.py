from typing import Any

import httpx


class BrowserWorkerClient:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url.rstrip("/"), token

    async def request(self, method: str, path: str, data: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            result = await client.request(method, self.url + path,
                                          headers={"Authorization": "Bearer " + self.token}, json=data)
            result.raise_for_status()
            return result.json()

    async def ask(self, prompt: str, request_id: str, *, session_id: str | None = None,
                  new_conversation: bool = False, attachments: list[dict] | None = None) -> dict:
        return await self.request("POST", "/ask", {"prompt": prompt, "request_id": request_id,
            "session_id": session_id, "new_conversation": new_conversation,
            "attachments": attachments or []})

    async def status(self, task_id: str) -> dict:
        return await self.request("GET", "/tasks/" + task_id)

    async def cancel(self, task_id: str) -> None:
        await self.request("POST", "/tasks/" + task_id + "/cancel")


class SearchClient:
    def __init__(self, provider: str, base_url: str, key: str) -> None:
        self.provider, self.base_url, self.key = provider, base_url.rstrip("/"), key

    async def search(self, query: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=20) as client:
            if self.provider == "tavily":
                response = await client.post(self.base_url + "/search", json={"api_key": self.key, "query": query, "max_results": 6})
            elif self.provider == "searxng":
                response = await client.get(self.base_url + "/search", params={"q": query, "format": "json"})
            else:
                raise ValueError("Unsupported search provider")
            response.raise_for_status()
            values = response.json().get("results", [])
            return [{"title": str(item.get("title", ""))[:200], "url": str(item.get("url", "")),
                     "snippet": str(item.get("content", ""))[:1500]} for item in values[:6]]
