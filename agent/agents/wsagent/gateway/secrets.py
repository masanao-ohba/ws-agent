"""Secret Manager access, read-only by design (no write path exists).
Lazy singleton; tests replace it via set_store()."""

import functools
import os

from .provider import SecretStore


class SecretManagerStore:
    def __init__(self, gcp_project: str):
        self._gcp_project = gcp_project
        self._cache: dict[str, str] = {}

    def get(self, secret_name: str) -> str:
        if secret_name not in self._cache:
            from google.cloud import secretmanager

            client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{self._gcp_project}/secrets/{secret_name}/versions/latest"
            self._cache[secret_name] = client.access_secret_version(
                name=name
            ).payload.data.decode()
        return self._cache[secret_name]



_override: SecretStore | None = None


def set_store(store: SecretStore | None) -> None:
    global _override
    _override = store
    _default.cache_clear()


@functools.cache
def _default() -> SecretStore:
    return SecretManagerStore(os.environ["GOOGLE_CLOUD_PROJECT"])


def store() -> SecretStore:
    return _override if _override is not None else _default()
