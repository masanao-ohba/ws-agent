"""TokenProvider: unattended access-token renewal for all outbound connections.

One implementation per auth style, one interface. Secrets are resolved from
Secret Manager lazily and cached in memory; access tokens are cached until
5 minutes before expiry. All failures surface as AuthError, which the tool
layer converts to an envelope failure (auth_expired) — never an exception
escaping to the LLM.
"""

import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx

_EXPIRY_MARGIN_SEC = 300


class AuthError(Exception):
    def __init__(self, service: str, project_id: str, detail: str):
        super().__init__(f"{service}/{project_id}: {detail}")
        self.service = service
        self.project_id = project_id


class SecretStore(Protocol):
    """Abstraction over Secret Manager so tests can substitute a dict.

    Read-only: credentials are static values managed by humans, never
    rewritten by the runtime.
    """

    def get(self, secret_name: str) -> str: ...


@dataclass
class _Cached:
    token: str
    expires_at: float


@dataclass
class OAuthRefresher:
    """OAuth2 refresh_token grant (GWS)."""

    service: str
    token_endpoint: str
    client_id: str
    client_secret: str
    secrets: SecretStore
    _cache: dict[str, _Cached] = field(default_factory=dict)

    def get(self, project_id: str, refresh_token_secret: str) -> str:
        cached = self._cache.get(project_id)
        if cached and cached.expires_at - _EXPIRY_MARGIN_SEC > time.time():
            return cached.token
        refresh_token = self.secrets.get(refresh_token_secret)
        resp = httpx.post(
            self.token_endpoint,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise AuthError(self.service, project_id, f"refresh failed: HTTP {resp.status_code}")
        body = resp.json()
        # Some providers answer HTTP 200 with ok=false; a status check alone
        # would pass.
        if body.get("ok") is False:
            raise AuthError(self.service, project_id, f"refresh failed: {body.get('error')}")
        token = str(body["access_token"])
        self._cache[project_id] = _Cached(token, time.time() + int(body.get("expires_in", 3600)))
        return token

    def invalidate(self, project_id: str) -> None:
        self._cache.pop(project_id, None)


@dataclass
class GithubAppTokens:
    """GitHub App: JWT signed with the app private key, exchanged for an
    installation access token (1h). Same get/invalidate surface as OAuthRefresher."""

    app_id: int
    private_key_secret: str
    secrets: SecretStore
    _cache: dict[str, _Cached] = field(default_factory=dict)

    def get(self, project_id: str, installation_id: int) -> str:
        cached = self._cache.get(project_id)
        if cached and cached.expires_at - _EXPIRY_MARGIN_SEC > time.time():
            return cached.token
        jwt = self._signed_jwt()
        resp = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {jwt}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        if resp.status_code != 201:
            raise AuthError("github", project_id, f"installation token: HTTP {resp.status_code}")
        token = str(resp.json()["token"])
        self._cache[project_id] = _Cached(token, time.time() + 3600)
        return token

    def invalidate(self, project_id: str) -> None:
        self._cache.pop(project_id, None)

    def _signed_jwt(self) -> str:
        raise NotImplementedError(
            "GitHub App path not implemented: RS256 JWT signing "
            "(private key from Secret Manager)"
        )
