import base64
import hashlib
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.oidc import OidcClient, OidcError, PkcePair
from app.ui.auth_routes import safe_next

ISSUER = "http://idp.example/realms/test"
CLIENT = "lamplighter"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJwks:
    def get_signing_key_from_jwt(self, _token: str) -> Any:
        class K:
            key = KEY.public_key()

        return K()


@pytest.fixture
def client() -> OidcClient:
    c = OidcClient(
        issuer=ISSUER,
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=CLIENT,
        client_secret="s",
        audience=CLIENT,
    )
    c._metadata = {
        "authorization_endpoint": f"{ISSUER}/auth",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/certs",
    }
    c._metadata_at = time.monotonic()
    c._jwks = FakeJwks()  # type: ignore[assignment]
    return c


def token(key: Any = KEY, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": [CLIENT, "account"],
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
        "preferred_username": "alice",
        "resource_access": {CLIENT: {"roles": ["operator"]}, "account": {"roles": ["x"]}},
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def test_valid_access_token(client: OidcClient) -> None:
    claims = client.validate_access_token(token())
    assert claims["sub"] == "user-1"
    assert client.roles(claims) == ["operator"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": int(time.time()) - 120},  # verlopen (buiten leeway)
        {"aud": "account"},  # andere audience
        {"iss": "http://evil.example/realms/test"},
        {"exp": None},  # verplichte claim ontbreekt
        {"sub": None},
    ],
)
def test_invalid_access_tokens(client: OidcClient, overrides: dict[str, Any]) -> None:
    with pytest.raises(OidcError):
        client.validate_access_token(token(**overrides))


def test_wrong_signature(client: OidcClient) -> None:
    with pytest.raises(OidcError):
        client.validate_access_token(token(key=OTHER_KEY))


def test_hs256_with_public_key_is_rejected(client: OidcClient) -> None:
    forged = jwt.encode(
        {"iss": ISSUER, "aud": CLIENT, "sub": "x", "iat": 1, "exp": int(time.time()) + 60},
        "x" * 32,
        algorithm="HS256",
    )
    with pytest.raises(OidcError):
        client.validate_access_token(forged)


def test_garbage_token(client: OidcClient) -> None:
    with pytest.raises(OidcError):
        client.validate_access_token("abc.def.ghi")


def test_id_token_nonce(client: OidcClient) -> None:
    good = token(aud=CLIENT, nonce="n-123")
    assert client.validate_id_token(good, "n-123")["sub"] == "user-1"
    with pytest.raises(OidcError, match="nonce"):
        client.validate_id_token(good, "other")
    with pytest.raises(OidcError, match="nonce"):
        client.validate_id_token(token(aud=CLIENT), "n-123")


@pytest.mark.parametrize(
    "resource_access",
    [None, "x", {}, {CLIENT: "x"}, {CLIENT: {"roles": "admin"}}, {"other": {"roles": ["admin"]}}],
)
def test_roles_are_robust(client: OidcClient, resource_access: Any) -> None:
    assert client.roles({"resource_access": resource_access}) == []


def test_pkce_challenge_is_s256_of_verifier() -> None:
    pair = PkcePair.new()
    digest = hashlib.sha256(pair.verifier.encode()).digest()
    assert pair.challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(pair.verifier) <= 128


def test_authorize_url(client: OidcClient) -> None:
    url = client.authorize_url(
        redirect_uri="http://app/cb", state="st", nonce="no", pkce=PkcePair("v", "c")
    )
    assert url.startswith(f"{ISSUER}/auth?")
    for part in ("code_challenge=c", "code_challenge_method=S256", "state=st", "nonce=no"):
        assert part in url


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/ui/templates", "/ui/templates"),
        ("/ui/runs?status=failed", "/ui/runs?status=failed"),
        (None, "/ui/dashboard"),
        ("https://evil.example/ui", "/ui/dashboard"),
        ("//evil.example/ui", "/ui/dashboard"),
        ("/api/v1/users", "/ui/dashboard"),
        ("/ui\\@evil.example", "/ui/dashboard"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


def test_logout_url(client: OidcClient) -> None:
    client._metadata["end_session_endpoint"] = f"{ISSUER}/logout"
    back = "https://ll.example.org/ui/login?signed_out=1"
    with_hint = client.logout_url(id_token="ID.TOKEN.X", post_logout_redirect_uri=back)
    assert with_hint is not None
    params = parse_qs(urlsplit(with_hint).query)
    assert with_hint.startswith(f"{ISSUER}/logout?")
    assert params == {
        "client_id": [CLIENT],
        "post_logout_redirect_uri": [back],
        "id_token_hint": ["ID.TOKEN.X"],
    }
    without = client.logout_url(id_token=None, post_logout_redirect_uri=back)
    assert without is not None
    assert "id_token_hint" not in parse_qs(urlsplit(without).query)


def test_logout_url_without_end_session_endpoint(client: OidcClient) -> None:
    assert client.logout_url(id_token="x", post_logout_redirect_uri="https://x/ui/login") is None
