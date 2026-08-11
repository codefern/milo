from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ResearchError(ValueError):
    """A research source is unsafe, unreachable, or unsupported."""


@dataclass(frozen=True)
class Source:
    url: str
    title: str
    content: str


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ResearchError("research URLs must use HTTPS")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ResearchError(f"cannot resolve source host: {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ResearchError("research URL resolves to a non-public network")
    return url


def fetch_source(url: str, *, max_bytes: int = 1_000_000, timeout: int = 20) -> Source:
    validate_public_url(url)
    request = Request(url, headers={"User-Agent": "Milo/0.1 (+https://github.com/codefern/milo)"})
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is validated above
            final_url = validate_public_url(response.geturl())
            content_type = response.headers.get_content_type()
            if not (
                content_type.startswith("text/")
                or content_type in {"application/json", "application/xml"}
            ):
                raise ResearchError(f"unsupported content type: {content_type}")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ResearchError("source exceeds the configured size limit")
            text = raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except OSError as exc:
        raise ResearchError(f"could not retrieve source: {exc}") from exc
    return Source(final_url, urlparse(final_url).hostname or final_url, text)


def synthesize_sources(topic: str, sources: list[Source]) -> str:
    if not sources:
        raise ResearchError("at least one retrieved source is required")
    sections = [f"# Research: {topic}"]
    for index, source in enumerate(sources, 1):
        excerpt = " ".join(source.content.split())[:1200]
        sections.append(f"[{index}] {source.title}\n{excerpt}\nSource: {source.url}")
    sections.append("Sources:\n" + "\n".join(f"[{i}] {s.url}" for i, s in enumerate(sources, 1)))
    return "\n\n".join(sections)
