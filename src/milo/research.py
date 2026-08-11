from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse


class ResearchError(ValueError):
    """A research source is unsafe, unreachable, or unsupported."""


@dataclass(frozen=True)
class Source:
    url: str
    title: str
    content: str


def _public_target(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ResearchError("research URLs must use credential-free HTTPS")
    port = parsed.port or 443
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ResearchError(f"cannot resolve source host: {parsed.hostname}") from exc
    resolved = sorted({str(address[4][0]) for address in addresses})
    if not resolved:
        raise ResearchError(f"cannot resolve source host: {parsed.hostname}")
    if any(not ipaddress.ip_address(address).is_global for address in resolved):
        raise ResearchError("research URL resolves to a non-public network")
    return parsed.hostname, port, resolved[0]


def validate_public_url(url: str) -> str:
    _public_target(url)
    return url


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS connection pinned to the IP address that passed policy validation."""

    def __init__(self, hostname: str, port: int, address: str, timeout: int) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(hostname, port=port, timeout=timeout, context=self._ssl_context)
        self._validated_address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._validated_address, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw, server_hostname=self.host)


def fetch_source(
    url: str, *, max_bytes: int = 1_000_000, timeout: int = 20, max_redirects: int = 3
) -> Source:
    current = url
    try:
        for redirect in range(max_redirects + 1):
            hostname, port, address = _public_target(current)
            parsed = urlparse(current)
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            connection = _PinnedHTTPSConnection(hostname, port, address, timeout)
            try:
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Host": parsed.netloc,
                        "User-Agent": "Milo/1.0 (+https://github.com/codefern/milo)",
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or redirect == max_redirects:
                        raise ResearchError("source redirect limit exceeded")
                    current = urljoin(current, location)
                    continue
                if response.status < 200 or response.status >= 300:
                    raise ResearchError(f"source returned HTTP {response.status}")
                content_type = response.headers.get_content_type()
                if not (
                    content_type.startswith("text/")
                    or content_type in {"application/json", "application/xml"}
                ):
                    raise ResearchError(f"unsupported content type: {content_type}")
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ResearchError("source exceeds the configured size limit")
                text = raw.decode(
                    response.headers.get_content_charset() or "utf-8", errors="replace"
                )
                return Source(current, hostname, text)
            finally:
                connection.close()
    except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
        raise ResearchError(f"could not retrieve source: {exc}") from exc
    raise ResearchError("source redirect limit exceeded")


def synthesize_sources(topic: str, sources: list[Source]) -> str:
    if not sources:
        raise ResearchError("at least one retrieved source is required")
    sections = [f"# Research: {topic}"]
    for index, source in enumerate(sources, 1):
        excerpt = " ".join(source.content.split())[:1200]
        sections.append(f"[{index}] {source.title}\n{excerpt}\nSource: {source.url}")
    sections.append("Sources:\n" + "\n".join(f"[{i}] {s.url}" for i, s in enumerate(sources, 1)))
    return "\n\n".join(sections)
