"""Login error classification: 5xx / 404 / 429 from the identity provider is transient.

VW's identity service intermittently answers the credentials POST with
HTTP 500 (a branded "general error" page), 404 (service redeploying) or 429
(rate limited). None of those is a credential problem, so they must surface as
a retryable ApiError (with status) rather than an AuthError — otherwise the
coordinator raises ConfigEntryAuthFailed and Home Assistant demands
reauthentication for an error that would clear on the next poll.
"""
from __future__ import annotations

import pytest

from custom_components.vw_eu_data_act.api import ApiError, AuthError, EudaApiClient

SIGNIN_HTML = """
<form action="/signin-service/v1/client@apps/login/identifier" method="POST">
  <input type="hidden" name="_csrf" value="csrf-1"/>
  <input type="hidden" name="hmac" value="hmac-1"/>
  <input type="hidden" name="relayState" value="relay-1"/>
</form>
"""

AUTHENTICATE_HTML = """
<form method="POST">
  <input type="hidden" name="_csrf" value="csrf-2"/>
  <input type="hidden" name="hmac" value="hmac-2"/>
  <input type="hidden" name="relayState" value="relay-1"/>
</form>
"""

ERROR_500_HTML = """
<!DOCTYPE html>
<html><head><meta name="identitykit" content="generalErrorBranded"/>
<title>Volkswagen ID</title></head><body></body></html>
"""


class FakeResponse:
    def __init__(self, status: int, text: str, url: str) -> None:
        self.status = status
        self._text = text
        self.url = url

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """Serves a scripted queue of responses for get/post alike."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)

    async def get(self, url, **kwargs) -> FakeResponse:
        return self._responses.pop(0)

    def post(self, url, **kwargs) -> FakeResponse:
        return self._responses.pop(0)


def _login_responses(final: FakeResponse) -> list[FakeResponse]:
    """The three responses _do_login consumes: authorize, identifier, credentials."""
    return [
        FakeResponse(200, SIGNIN_HTML, "https://identity.vwgroup.io/signin-service/v1/x/login"),
        FakeResponse(
            200,
            AUTHENTICATE_HTML,
            "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate?relayState=relay-1",
        ),
        final,
    ]


@pytest.mark.parametrize("status", [500, 502, 503, 404, 429])
async def test_login_transient_status_raises_retryable_api_error(status: int) -> None:
    session = FakeSession(
        _login_responses(
            FakeResponse(
                status,
                ERROR_500_HTML,
                "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate",
            )
        )
    )
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(ApiError) as excinfo:
        await client.async_login()

    assert not isinstance(excinfo.value, AuthError)
    assert excinfo.value.status == status


@pytest.mark.parametrize("status", [404, 429, 503])
async def test_login_transient_status_on_signin_page_is_retryable(status: int) -> None:
    # Failure at step 1 (authorize -> sign-in page) must not be misread as a
    # broken form / bad credentials.
    session = FakeSession(
        [
            FakeResponse(status, "", "https://identity.vwgroup.io/signin-service/v1/x/login"),
        ]
    )
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(ApiError) as excinfo:
        await client.async_login()

    assert not isinstance(excinfo.value, AuthError)
    assert excinfo.value.status == status


@pytest.mark.parametrize("status", [404, 429, 503])
async def test_login_transient_status_on_identifier_step_is_retryable(status: int) -> None:
    session = FakeSession(
        [
            FakeResponse(200, SIGNIN_HTML, "https://identity.vwgroup.io/signin-service/v1/x/login"),
            FakeResponse(
                status,
                "",
                "https://identity.vwgroup.io/signin-service/v1/x/login/identifier",
            ),
        ]
    )
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(ApiError) as excinfo:
        await client.async_login()

    assert not isinstance(excinfo.value, AuthError)
    assert excinfo.value.status == status


async def test_login_other_4xx_still_raises_auth_error() -> None:
    # A plain 4xx that is not in the transient set (e.g. 403) stays an AuthError.
    session = FakeSession(
        _login_responses(
            FakeResponse(
                403,
                "",
                "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate",
            )
        )
    )
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(AuthError):
        await client.async_login()


async def test_login_bad_credentials_still_raises_auth_error() -> None:
    # Bad credentials re-render the identity sign-in page with HTTP 200.
    session = FakeSession(
        _login_responses(
            FakeResponse(
                200,
                SIGNIN_HTML,
                "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate",
            )
        )
    )
    client = EudaApiClient(session, "user@example.com", "wrong-password")

    with pytest.raises(AuthError):
        await client.async_login()
