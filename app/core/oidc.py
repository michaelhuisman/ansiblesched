"""OIDC (Keycloak): discovery, JWKS, token validation and the authorization code flow with PKCE.

Two addresses for the same IdP: the browser uses the public address (issuer and
authorization endpoint), the server uses the internal address from discovery for token
and JWKS. Keycloak provides that with `hostname-backchannel-dynamic`.
"""

import base64
import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from app.core.config import Settings

DISCOVERY_TTL_S = 3600
HTTP_TIMEOUT_S = 10.0
ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]
LEEWAY_S = 30

# Claims from a validated token: JSON from the IdP.
Claims = dict[str, Any]


class OidcError(Exception):
    """Invalid token or flow. The message is safe for logs, not for users."""


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str

    @classmethod
    def new(cls) -> "PkcePair":
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return cls(verifier, challenge)


@dataclass
class OidcClient:
    issuer: str
    discovery_url: str
    client_id: str
    client_secret: str
    audience: str
    _metadata: dict[str, Any] = field(default_factory=dict)
    _metadata_at: float = 0.0
    _jwks: jwt.PyJWKClient | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_settings(cls, settings: Settings) -> "OidcClient | None":
        if not settings.oidc_enabled or settings.oidc_issuer is None:
            return None
        if settings.oidc_client_secret is None:  # covered by oidc_enabled
            return None
        issuer = settings.oidc_issuer.rstrip("/")
        return cls(
            issuer=issuer,
            discovery_url=settings.oidc_discovery_url
            or f"{issuer}/.well-known/openid-configuration",
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret.get_secret_value(),
            audience=settings.oidc_audience or settings.oidc_client_id,
        )

    # --- metadata and keys ----------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            if not self._metadata or time.monotonic() - self._metadata_at > DISCOVERY_TTL_S:
                resp = httpx.get(self.discovery_url, timeout=HTTP_TIMEOUT_S)
                resp.raise_for_status()
                self._metadata = resp.json()
                self._metadata_at = time.monotonic()
                self._jwks = jwt.PyJWKClient(
                    self._metadata["jwks_uri"], cache_keys=True, lifespan=300
                )
            return self._metadata

    def _signing_key(self, token: str) -> Any:  # jwt.PyJWK
        self.metadata()
        if self._jwks is None:
            raise OidcError("JWKS client not initialised")
        try:
            return self._jwks.get_signing_key_from_jwt(token).key
        except (jwt.PyJWKClientError, jwt.DecodeError) as exc:
            raise OidcError(f"no usable signing key: {type(exc).__name__}") from exc

    # --- validation -----------------------------------------------------------------

    def _decode(self, token: str, audience: str) -> Claims:
        try:
            claims: Claims = jwt.decode(
                token,
                key=self._signing_key(token),
                algorithms=ALGORITHMS,
                audience=audience,
                issuer=self.issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"invalid token: {type(exc).__name__}") from exc
        return claims

    def validate_access_token(self, token: str) -> Claims:
        return self._decode(token, self.audience)

    def validate_id_token(self, token: str, nonce: str) -> Claims:
        claims = self._decode(token, self.client_id)
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("nonce mismatch")
        return claims

    def roles(self, claims: Claims) -> list[str]:
        """Client roles of our client from `resource_access`."""
        access = claims.get("resource_access")
        if not isinstance(access, dict):
            return []
        client = access.get(self.client_id)
        roles = client.get("roles") if isinstance(client, dict) else None
        return [r for r in roles if isinstance(r, str)] if isinstance(roles, list) else []

    # --- authorization code flow ----------------------------------------------------

    def authorize_url(self, *, redirect_uri: str, state: str, nonce: str, pkce: PkcePair) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email",
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce.challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.metadata()['authorization_endpoint']}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
        resp = httpx.post(
            self.metadata()["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            auth=(self.client_id, self.client_secret),
            timeout=HTTP_TIMEOUT_S,
        )
        if resp.status_code != 200:
            raise OidcError(f"token endpoint returned {resp.status_code}")
        body: dict[str, Any] = resp.json()
        if "id_token" not in body or "access_token" not in body:
            raise OidcError("token response misses id_token or access_token")
        return body
