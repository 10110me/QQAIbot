import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_reserved


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError("Only credential-free public HTTP(S) URLs are allowed")
    if parsed.port not in {None, 80, 443}:
        raise PermissionError("Download port not allowed")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith((".local", ".internal", ".localhost")):
        raise PermissionError("Private hostname denied")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    if not public_ip(str(address)):
        raise PermissionError("Private or reserved address denied")
    return hostname


class PublicResolver(AbstractResolver):
    """Resolve once per connection; only the returned vetted IPs reach the socket."""

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC) -> list[dict]:
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        if not addresses or any(not public_ip(value[4][0]) for value in addresses):
            raise PermissionError("DNS resolved to a private or reserved address")
        return [{"hostname": host, "host": value[4][0], "port": port, "family": value[0],
                 "proto": value[2], "flags": socket.AI_NUMERICHOST} for value in addresses]

    async def close(self) -> None:
        pass


class PublicDownloader:
    def __init__(self, limit: int = 20_000_000, redirects: int = 3) -> None:
        self.limit, self.redirects = limit, redirects

    async def fetch(self, url: str, max_bytes: int | None = None) -> tuple[bytes, str, str]:
        content, mime, final, _ = await self.fetch_response(url, max_bytes)
        return content, mime, final

    async def fetch_response(self, url: str, max_bytes: int | None = None, *,
                             render_http_errors: bool = False, browser_headers: dict | None = None) -> tuple[bytes, str, str, int]:
        # A screenshot must render the actual 403/404 page, not discard it.
        # Keep normal file download behavior and all DNS/redirect limits intact.
        headers = {k:v for k,v in (browser_headers or {}).items()
                   if k.lower() in {'user-agent','accept','accept-language'} and isinstance(v,str)}
        headers['Accept-Encoding'] = 'identity'
        # Explicit zero is reserved for original QQ image uploads. Other
        # downloads retain the configured byte limit and all network checks.
        limit = None if max_bytes == 0 else min(self.limit, max_bytes or self.limit)
        connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, limit=8)
        # Download worker intentionally uses direct, pinned DNS. A proxy resolving arbitrary
        # hostnames would defeat this SSRF boundary; network allowlists belong outside it.
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                        timeout=aiohttp.ClientTimeout(total=45), auto_decompress=False) as client:
            for hop in range(self.redirects + 1):
                validate_url(url)
                async with client.get(url, allow_redirects=False, headers=headers) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if hop == self.redirects or not response.headers.get("Location"):
                            raise ValueError("Too many or invalid redirects")
                        url = urljoin(url, response.headers["Location"])
                        continue
                    if not render_http_errors:
                        response.raise_for_status()
                    if response.headers.get("Content-Encoding", "identity") not in {"identity", ""}:
                        raise ValueError("Unexpected compressed transfer")
                    if limit is not None and response.content_length is not None and response.content_length > limit:
                        raise ValueError("Download exceeds configured size")
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if limit is not None and size > limit:
                            raise ValueError("Streaming download exceeds configured size")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("Content-Type", "application/octet-stream").split(";")[0], url, response.status
        raise ValueError("Download did not complete")
