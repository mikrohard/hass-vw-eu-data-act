"""Login flow: redirect handling and error classification.

Two concerns:

* After the credentials POST the identity provider bounces the client back to
  the portal, whose ``/services/callbacklogin`` sets the ``access_token``
  session cookie and then redirects to a localized CMS landing page. The
  client follows that chain by hand and stops at the callback: the CMS page is
  irrelevant to authentication (and 404s for some locales), and VW asked
  integrations not to fetch it.

* VW's identity service intermittently answers a login step with HTTP 500 (a
  branded "general error" page), 404 (service redeploying) or 429 (rate
  limited). None of those is a credential problem, so they must surface as a
  retryable ApiError (with status) rather than an AuthError — otherwise the
  coordinator raises ConfigEntryAuthFailed and Home Assistant demands
  reauthentication for an error that would clear on the next poll.
"""
from __future__ import annotations

import pytest

from custom_components.vw_eu_data_act.api import ApiError, AuthError, EudaApiClient
from custom_components.vw_eu_data_act.const import BASE_URL, CALLBACK_LOGIN_PATH

AUTHENTICATE_URL = "https://identity.vwgroup.io/signin-service/v1/x/login/authenticate"
CALLBACK_URL = f"{BASE_URL}{CALLBACK_LOGIN_PATH}?code=abc&state=xyz"
CMS_LANDING_URL = f"{BASE_URL}/content/euda/de/de/user.html"

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
    def __init__(
        self,
        status: int,
        text: str,
        url: str,
        *,
        location: str | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._text = text
        self.url = url
        self.headers = {"Location": location} if location else {}
        self.cookies = cookies or {}
        self.released = False

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode()

    def release(self) -> None:
        self.released = True

    def __await__(self):
        # aiohttp's request context manager is awaitable *and* usable with
        # ``async with``; the client uses both forms.
        async def _self():
            return self

        return _self().__await__()

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """Serves a scripted queue of responses for get/post alike, recording each call."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict]] = []

    async def get(self, url, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def post(self, url, **kwargs) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)


def _login_responses(*after_credentials: FakeResponse) -> list[FakeResponse]:
    """Responses _do_login consumes: authorize, identifier, then the credentials chain."""
    return [
        FakeResponse(200, SIGNIN_HTML, "https://identity.vwgroup.io/signin-service/v1/x/login"),
        FakeResponse(200, AUTHENTICATE_HTML, f"{AUTHENTICATE_URL}?relayState=relay-1"),
        *after_credentials,
    ]


def _callback_response(**cookies: str) -> FakeResponse:
    return FakeResponse(302, "", CALLBACK_URL, location=CMS_LANDING_URL, cookies=cookies)


async def test_login_stops_at_portal_callback_and_skips_cms_page() -> None:
    # authenticate -> 303 to portal /login -> 302 to callbacklogin (sets the
    # session cookie, redirects to the CMS page). The CMS page is never fetched.
    hop1 = FakeResponse(
        303, "", AUTHENTICATE_URL, location=f"{BASE_URL}/login?code=abc&state=xyz"
    )
    hop2 = FakeResponse(
        302, "", f"{BASE_URL}/login?code=abc&state=xyz", location=CALLBACK_LOGIN_PATH + "?code=abc&state=xyz"
    )
    callback = _callback_response(access_token="jwt")
    session = FakeSession(_login_responses(hop1, hop2, callback))
    client = EudaApiClient(session, "user@example.com", "secret")

    await client.async_login()

    methods_and_urls = [(m, u) for m, u, _ in session.calls]
    assert methods_and_urls[2:] == [
        ("POST", AUTHENTICATE_URL),
        ("GET", f"{BASE_URL}/login?code=abc&state=xyz"),
        ("GET", CALLBACK_URL),
    ]
    assert all(kw["allow_redirects"] is False for _, _, kw in session.calls[2:])
    # Each hop carries the previous URL as Referer, like a browser would.
    assert session.calls[3][2]["headers"]["Referer"] == AUTHENTICATE_URL
    assert session.calls[4][2]["headers"]["Referer"] == f"{BASE_URL}/login?code=abc&state=xyz"
    assert hop1.released and hop2.released
    assert not callback.released  # handed back to the caller's ``async with``


async def test_login_callback_without_session_cookie_is_auth_error() -> None:
    session = FakeSession(_login_responses(_callback_response()))
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(AuthError, match="access_token"):
        await client.async_login()


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_login_callback_server_error_is_retryable(status: int) -> None:
    # The code exchange failing on the portal side is not a credential problem.
    failing_callback = FakeResponse(status, "", CALLBACK_URL)
    session = FakeSession(_login_responses(failing_callback))
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(ApiError) as excinfo:
        await client.async_login()

    assert not isinstance(excinfo.value, AuthError)
    assert excinfo.value.status == status


async def test_login_redirect_loop_is_retryable_api_error() -> None:
    loop_url = "https://identity.vwgroup.io/signin-service/v1/x/loop"
    hops = [FakeResponse(302, "", loop_url, location=loop_url) for _ in range(12)]
    session = FakeSession(_login_responses(*hops))
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(ApiError) as excinfo:
        await client.async_login()

    assert not isinstance(excinfo.value, AuthError)
    assert all(h.released for h in hops[:11])


async def test_login_redirect_without_location_ends_chain() -> None:
    # A 302 with no Location header cannot be followed; it is judged as the
    # final response (not on the portal, so the login did not complete).
    stuck_url = "https://identity.vwgroup.io/oidc/v1/oauth/sso?clientId=x"
    session = FakeSession(_login_responses(FakeResponse(302, "", stuck_url)))
    client = EudaApiClient(session, "user@example.com", "secret")

    with pytest.raises(AuthError, match="did not complete"):
        await client.async_login()


@pytest.mark.parametrize("status", [500, 502, 503, 404, 429])
async def test_login_transient_status_raises_retryable_api_error(status: int) -> None:
    session = FakeSession(
        _login_responses(
            FakeResponse(
                status,
                ERROR_500_HTML,
                AUTHENTICATE_URL,
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
                AUTHENTICATE_URL,
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
                AUTHENTICATE_URL,
            )
        )
    )
    client = EudaApiClient(session, "user@example.com", "wrong-password")

    with pytest.raises(AuthError):
        await client.async_login()
