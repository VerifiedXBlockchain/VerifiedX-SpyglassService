"""Fetch a URL a client handed in, without letting it reach the server's own network.

Every connection, including each redirect hop, resolves the host itself and
refuses it unless every address it resolves to is public, then connects to the
address it checked. A DNS answer cannot change between the check and the
connect.
"""

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_REDIRECTS = 5
# The size UploadAssetView accepts (api/media/views.py MAX_FILE_SIZE, 150 MiB).
MAX_FETCH_BYTES = 157286400
CHUNK_SIZE = 64 * 1024

ALLOWED_SCHEMES = ("http", "https")
NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")


class FetchError(Exception):
    """A URL that was not fetched. `reason` is a short machine-readable code."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


class UnsafeUrl(FetchError):
    """The URL was refused before or while connecting: the client's fault."""


class FetchFailed(FetchError):
    """The URL was allowed but could not be fetched."""


def _embedded_ipv4(ip):
    if ip.version != 6:
        return None
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in NAT64_PREFIX:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_address(address: str) -> bool:
    """False for private, loopback, link-local (169.254.169.254), CGNAT, multicast and reserved ranges."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    embedded = _embedded_ipv4(ip)
    if embedded is not None and not is_public_address(str(embedded)):
        return False
    return ip.is_global and not ip.is_multicast


def resolve_public(host: str, port: int) -> list:
    """Every address `host` resolves to, or UnsafeUrl when any of them is not public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchFailed("unresolvable", f"Could not resolve host {host!r}: {e}")
    if not infos:
        raise FetchFailed("unresolvable", f"Could not resolve host {host!r}")
    for _family, _type, _proto, _canon, sockaddr in infos:
        if not is_public_address(sockaddr[0]):
            # The address itself is left out: it would map internal names for the caller.
            raise UnsafeUrl(
                "blocked_address", f"Host {host!r} does not resolve to a public address."
            )
    return infos


class _PublicConnectionMixin:
    def _new_conn(self):
        infos = resolve_public(self._dns_host, self.port)
        last_error = None
        for family, socktype, proto, _canon, sockaddr in infos:
            sock = socket.socket(family, socktype, proto)
            try:
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                sock.close()
                last_error = e
        if isinstance(last_error, socket.timeout):
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out.")
        raise NewConnectionError(self, f"Failed to establish a new connection: {last_error}")


class _PublicHTTPConnection(_PublicConnectionMixin, HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicConnectionMixin, HTTPSConnection):
    pass


class _PublicHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PublicHTTPConnection


class _PublicHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _PublicOnlyAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _PublicHTTPConnectionPool,
            "https": _PublicHTTPSConnectionPool,
        }


def _public_session() -> requests.Session:
    session = requests.Session()
    # No proxy from the environment: a proxy would connect for the server, unchecked.
    session.trust_env = False
    adapter = _PublicOnlyAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def check_url(url: str) -> None:
    """UnsafeUrl unless `url` is an http(s) URL with a host."""
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrl("scheme", f"Only http and https URLs can be fetched, not {url!r}.")
    if not parsed.hostname:
        raise UnsafeUrl("invalid_url", f"URL {url!r} has no host.")


def fetch_to_file(url: str, file, max_bytes: int = MAX_FETCH_BYTES) -> int:
    """Stream `url` into the open binary `file` and return the number of bytes written.

    Raises UnsafeUrl when the URL, a redirect or the size is refused, and
    FetchFailed when the host cannot be reached, times out or answers an error.
    """
    with _public_session() as session:
        for _hop in range(MAX_REDIRECTS + 1):
            check_url(url)
            try:
                response = session.get(
                    url,
                    stream=True,
                    allow_redirects=False,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                )
            except requests.Timeout as e:
                raise FetchFailed("timeout", f"Timed out fetching {url!r}: {e}")
            except requests.RequestException as e:
                raise FetchFailed("unreachable", f"Could not fetch {url!r}: {e}")

            with response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["Location"])
                    continue
                return _write_body(url, response, file, max_bytes)

    raise UnsafeUrl("too_many_redirects", f"More than {MAX_REDIRECTS} redirects.")


def _write_body(url, response, file, max_bytes) -> int:
    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise FetchFailed("http_status", f"{url!r} answered HTTP {response.status_code}.")

    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise UnsafeUrl("too_large", f"{url!r} is larger than {max_bytes} bytes.")

    written = 0
    try:
        for chunk in response.iter_content(CHUNK_SIZE):
            written += len(chunk)
            if written > max_bytes:
                raise UnsafeUrl("too_large", f"{url!r} is larger than {max_bytes} bytes.")
            file.write(chunk)
    except requests.Timeout as e:
        raise FetchFailed("timeout", f"Timed out reading {url!r}: {e}")
    except requests.RequestException as e:
        raise FetchFailed("unreachable", f"Lost the connection reading {url!r}: {e}")
    return written
