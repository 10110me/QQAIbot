"""Syntactic public URL boundary; network clients must also pin public DNS."""
import ipaddress
from urllib.parse import urlsplit


def validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
        raise PermissionError('Only credential-free public HTTP(S) URLs are allowed')
    if parsed.port not in {None,80,443}:
        raise PermissionError('Download port not allowed')
    hostname=parsed.hostname.rstrip('.').lower()
    if hostname in {'localhost','metadata.google.internal'} or hostname.endswith(('.local','.internal','.localhost')):
        raise PermissionError('Private hostname denied')
    try:
        address=ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    if getattr(address,'ipv4_mapped',None):address=address.ipv4_mapped
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise PermissionError('Private or reserved address denied')
    return hostname
