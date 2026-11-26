"""Authenticated access to the GitHub REST API as a GitHub App installation.

The App's private key never leaves this host: the JWT is signed by `openssl`, which reads the key file
itself, and only short-lived installation tokens travel over the network. Conditional requests with
ETags make the 15-second polling cheap, because a `304 Not Modified` does not count against the primary
rate limit.
"""

import base64
import datetime as dt
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import IO, Protocol, cast

from pve_ci_runners._version import VERSION
from pve_ci_runners.errors import CommandError, GitHubError
from pve_ci_runners.proc import Commands, output

_TIMEOUT = 30.0
_RETRIES = 3
_TOKEN_MARGIN = 300
_ERROR_TAIL = 200
_HTTP_NOT_MODIFIED = 304


@dataclass(frozen=True)
class Response:
    """One HTTP answer."""

    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    """Sends one HTTP request; the client adds authentication, caching and retries on top."""

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        """Sends the request and returns the response, also for error statuses."""
        ...


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would resend the Authorization header to wherever a redirect points; the API needs none."""

    def redirect_request(  # noqa: PLR0917, PLR6301 - the signature urllib calls
        self,
        req: urllib.request.Request,
        fp: IO[bytes],  # noqa: ARG002 - part of the signature urllib calls
        code: int,
        msg: str,  # noqa: ARG002 - part of the signature urllib calls
        headers: Message,  # noqa: ARG002 - part of the signature urllib calls
        newurl: str,  # noqa: ARG002 - part of the signature urllib calls
    ) -> urllib.request.Request | None:
        """Refuses every redirect."""
        raise GitHubError(f"{req.get_method()} {req.full_url.split('?', 1)[0]} was redirected ({code})")


class UrllibTransport:
    """The standard-library transport with a fixed timeout that never follows a redirect."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_RefuseRedirects)

    def send(self, method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
        """Sends the request and returns the response, also for error statuses."""
        request = urllib.request.Request(url, method=method, data=body, headers=headers)  # noqa: S310 - https only, enforced by the configuration
        try:
            with self._opener.open(request, timeout=_TIMEOUT) as answer:
                return Response(answer.status, dict(answer.headers.items()), answer.read())
        except urllib.error.HTTPError as err:
            return Response(err.code, dict(err.headers.items()), err.read())
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise GitHubError(
                f"{method} {url.split('?', 1)[0]} could not be sent: {err}", transient=True
            ) from None


class AppKeySigner:
    """Signs App JWTs with `openssl`, so the key is read by openssl and never by this process."""

    def __init__(self, commands: Commands, key_file: Path) -> None:
        self._commands = commands
        self._key_file = key_file

    def sign(self, message: bytes) -> bytes:
        """The RS256 signature of the message."""
        try:
            out = output(
                self._commands,
                ["openssl", "dgst", "-sha256", "-hex", "-sign", str(self._key_file)],
                stdin=message.decode("ascii"),
                timeout=30,
            )
        except CommandError as err:
            raise GitHubError(f"the App key could not sign a token: {err}") from None
        return bytes.fromhex(out.rsplit("=", 1)[-1].strip())


class Signer(Protocol):
    """Produces RS256 signatures with the App's private key."""

    def sign(self, message: bytes) -> bytes:
        """The RS256 signature of the message."""
        ...


@dataclass
class _Cached:
    etag: str
    payload: object


@dataclass
class GitHubClient:
    """JSON requests as the App installation of one account."""

    api_url: str
    client_id: str
    owner: str
    signer: Signer
    transport: Transport = field(default_factory=UrllibTransport)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    _token: str = field(default="", init=False, repr=False)
    _expires: float = field(default=0.0, init=False)
    _permissions: dict[str, str] = field(default_factory=dict[str, str], init=False)
    _cache: dict[str, _Cached] = field(default_factory=dict[str, _Cached], init=False, repr=False)
    # The controller reads several repositories at once; the token and the ETag cache are shared.
    _token_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def get(self, path: str, *, conditional: bool = False) -> object:
        """A GET that is retried on server errors; conditional GETs reuse the last answer on 304."""
        return self._request("GET", path, None, conditional=conditional)

    def post(self, path: str, body: object) -> object:
        """A POST, never retried, because it may not be idempotent."""
        return self._request("POST", path, body)

    def patch(self, path: str, body: object) -> object:
        """A PATCH, never retried."""
        return self._request("PATCH", path, body)

    def delete(self, path: str) -> object:
        """A DELETE, never retried."""
        return self._request("DELETE", path, None)

    def _request(self, method: str, path: str, body: object, *, conditional: bool = False) -> object:
        retries = _RETRIES - 1 if method == "GET" else 0
        for attempt in range(retries):
            try:
                return self._once(method, path, body, conditional=conditional, auth="")
            except GitHubError as err:
                if not err.transient:
                    raise
                self.sleep(2.0**attempt)
        return self._once(method, path, body, conditional=conditional, auth="")

    def _once(self, method: str, path: str, body: object, *, conditional: bool, auth: str) -> object:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"pve-ci-runners/{VERSION}",
            "Authorization": f"Bearer {auth or self._installation_token()}",
        }
        with self._cache_lock:
            cached = self._cache.get(path) if conditional else None
        if cached is not None:
            headers["If-None-Match"] = cached.etag
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        answer = self.transport.send(method, self.api_url + path, headers, data)
        if answer.status == _HTTP_NOT_MODIFIED and cached is not None:
            return cached.payload
        return self._payload(method, path, answer, conditional=conditional)

    def _payload(self, method: str, path: str, answer: Response, *, conditional: bool) -> object:
        if answer.status >= 400:  # noqa: PLR2004 - HTTP client and server errors
            raise GitHubError(
                f"{method} {path.split('?', 1)[0]} answered {answer.status}: {_reason(answer.body)}",
                answer.status,
            )
        payload: object = json.loads(answer.body) if answer.body.strip() else None
        etag = answer.headers.get("ETag") or answer.headers.get("etag")
        if conditional and etag:
            with self._cache_lock:
                self._cache[path] = _Cached(etag, payload)
        return payload

    def _installation_token(self) -> str:
        # One refresh at a time: the others wait for it instead of each asking GitHub for a token.
        with self._token_lock:
            if self.clock() < self._expires - _TOKEN_MARGIN:
                return self._token
            return self._refreshed_token()

    def _refreshed_token(self) -> str:
        jwt = self._jwt()
        installations = self._once("GET", "/app/installations", None, conditional=False, auth=jwt)
        ids = [i.get("id") for i in _dicts(installations) if _login(i.get("account")) == self.owner.lower()]
        if not ids or not isinstance(ids[0], int):
            raise GitHubError(f"the App is not installed on {self.owner}")
        granted = self._once(
            "POST", f"/app/installations/{ids[0]}/access_tokens", {}, conditional=False, auth=jwt
        )
        if not isinstance(granted, dict) or not isinstance(granted.get("token"), str):
            raise GitHubError("GitHub returned no installation token")
        self._token = granted["token"]
        self._expires = dt.datetime.fromisoformat(str(granted["expires_at"])).timestamp()
        permissions = granted.get("permissions")
        self._permissions = (
            {str(k): str(v) for k, v in cast("dict[object, object]", permissions).items()}
            if isinstance(permissions, dict)
            else {}
        )
        return self._token

    def permissions(self) -> dict[str, str]:
        """What the installation token may do, as GitHub granted it."""
        self._installation_token()
        with self._token_lock:
            return dict(self._permissions)

    def _jwt(self) -> str:
        now = int(self.clock())
        head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        # Issued a minute in the past to tolerate clock skew; GitHub accepts at most ten minutes.
        claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.client_id}).encode())
        signing_input = head + b"." + claims
        return (signing_input + b"." + _b64url(self.signer.sign(signing_input))).decode()


def _b64url(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _reason(body: bytes) -> str:
    try:
        message = json.loads(body).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    return str(message)[:_ERROR_TAIL]


def _login(account: object) -> str:
    return (
        str(cast("dict[str, object]", account).get("login", "")).lower() if isinstance(account, dict) else ""
    )


def _dicts(value: object) -> list[dict[str, object]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
