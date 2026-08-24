"""One-time Google authorisation via the installed-app loopback flow.

Why not the device flow (the `oauth2.googleapis.com/device/code` one)? Google
restricts it to a short scope list -- for Drive that is only `drive.appdata`
and `drive.file`. `drive.file` sees only files the app itself created, so it
cannot read a GoodNotes backup folder. `drive.readonly` is not offered there at
all, which makes the device flow unusable for this tool no matter how the
client is configured.

The loopback redirect flow has no such restriction. It needs a **Desktop app**
OAuth client, spins up a throwaway HTTP server on 127.0.0.1, and exchanges the
returned code for a refresh token. PKCE is included because Google recommends
it for installed apps, where the client secret cannot really be kept secret.

Standard library only.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import hashlib
import http.server
import secrets
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

__all__ = [
    "pkce_pair",
    "build_auth_url",
    "exchange_code",
    "run_local_flow",
    "OAuthError",
]


class OAuthError(RuntimeError):
    pass


def pkce_pair() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for PKCE S256.

    The verifier is 43-128 unreserved characters per RFC 7636; the challenge is
    its SHA-256 digest, base64url-encoded with padding stripped.
    """
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_auth_url(
    *,
    client_id: str,
    redirect_uri: str,
    challenge: str,
    state: str,
    scopes: list[str],
) -> str:
    """The consent-screen URL.

    ``access_type=offline`` plus ``prompt=consent`` is what makes Google return
    a refresh token. Without both, a second authorisation of the same client
    returns only an access token and the flow silently produces nothing usable.
    """
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


@dataclasses.dataclass(frozen=True)
class GoogleIdentity:
    """Who signed in, from the id_token that came back with the code."""

    sub: str
    email: str
    email_verified: bool
    name: str = ""
    picture: str = ""


def decode_id_token(id_token: str) -> GoogleIdentity:
    """Read the claims out of Google's id_token.

    The signature is deliberately not checked. This token did not arrive from
    the browser -- it came back in the body of a TLS request *we* made to
    Google's token endpoint, using our own client secret. Google's own guidance
    is that a token obtained that way needs no local validation; the channel
    already proves the issuer. Verifying it would mean fetching and caching
    JWKS on every cold start for no additional assurance.

    An id_token arriving by any other route must never be passed to this.
    """
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise OAuthError("Google returned a malformed id_token")
    try:
        raw = parts[1]
        payload = json.loads(
            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        )
    except Exception as exc:  # noqa: BLE001
        raise OAuthError(f"Could not read Google's id_token: {exc}") from exc

    email = str(payload.get("email", "")).strip().lower()
    if not email:
        raise OAuthError(
            "Google did not return an email address. The 'email' scope is "
            "required to know who is signing in."
        )
    return GoogleIdentity(
        sub=str(payload.get("sub", "")),
        email=email,
        # Google sends this as a bool or the string "true" depending on age.
        email_verified=str(payload.get("email_verified", "")).lower() in ("true", "1")
        or payload.get("email_verified") is True,
        name=str(payload.get("name", "")),
        picture=str(payload.get("picture", "")),
    )


def exchange_code_full(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    verifier: str,
    redirect_uri: str,
    session: requests.Session | None = None,
) -> dict:
    """The whole token response: refresh_token, access_token, id_token."""
    http = session or requests
    response = http.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    payload = response.json()
    if response.status_code != 200:
        raise OAuthError(
            f"Token exchange failed ({response.status_code}): "
            f"{payload.get('error')} - {payload.get('error_description')}"
        )
    return payload


def exchange_code(**kwargs) -> str:
    """Swap the authorisation code for a refresh token."""
    payload = exchange_code_full(**kwargs)
    token = payload.get("refresh_token")
    if not token:
        raise OAuthError(
            "Google returned no refresh_token. This happens when the client has "
            "been authorised before; revoke it at "
            "https://myaccount.google.com/permissions and run auth again."
        )
    return token


_PAGE = """<!doctype html><meta charset="utf-8">
<title>{title}</title>
<style>body{{font:16px/1.6 -apple-system,system-ui,sans-serif;max-width:32rem;
margin:18vh auto;padding:0 1.5rem;color:#1d1c1a}}
@media(prefers-color-scheme:dark){{body{{background:#191918;color:#eceae6}}}}
h1{{font-size:1.25rem;margin:0 0 .5rem}}p{{color:#6d6a65;margin:0}}</style>
<h1>{title}</h1><p>{body}</p>"""


class _Callback(http.server.BaseHTTPRequestHandler):
    query: dict[str, str] = {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        # The browser also asks for /favicon.ico; ignore anything that is not
        # the redirect so it does not consume the one request we are waiting for.
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return

        type(self).query = params
        ok = "code" in params
        page = _PAGE.format(
            title="Authorised" if ok else "Authorisation failed",
            body=(
                "You can close this tab and return to the terminal."
                if ok
                else f"Google said: {params.get('error', 'unknown error')}"
            ),
        ).encode("utf-8")

        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args) -> None:
        """Keep the server quiet; the CLI does its own reporting."""


def run_local_flow(
    *,
    client_id: str,
    client_secret: str,
    scopes: list[str],
    timeout: int = 300,
    open_browser: bool = True,
) -> str:
    """Run the whole flow and return a refresh token."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)

    # Port 0 lets the OS pick a free port. Desktop-app clients accept any
    # 127.0.0.1 port, so nothing needs registering in Cloud Console.
    _Callback.query = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _Callback)
    server.timeout = timeout
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}"

    url = build_auth_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        challenge=challenge,
        state=state,
        scopes=scopes,
    )

    print("\nOpening your browser to authorise read-only Drive access.")
    print("If it does not open, paste this into a browser:\n")
    print(f"  {url}\n")
    if open_browser:
        webbrowser.open(url)

    try:
        while not _Callback.query:
            server.handle_request()  # returns after `timeout` with nothing
            if not _Callback.query:
                raise OAuthError(
                    f"No response within {timeout}s. Run auth again."
                )
    finally:
        server.server_close()

    result = _Callback.query
    if "error" in result:
        raise OAuthError(f"Google returned an error: {result['error']}")
    if result.get("state") != state:
        raise OAuthError("State mismatch — discarding the response.")

    return exchange_code(
        client_id=client_id,
        client_secret=client_secret,
        code=result["code"],
        verifier=verifier,
        redirect_uri=redirect_uri,
    )
