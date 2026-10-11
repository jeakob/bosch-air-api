"""Read-only HomeCom Easy cloud discovery using Python's standard library."""

import base64
import hashlib
import json
import re
import secrets
from getpass import getpass
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


CLIENT_ID = "762162C0-FA2D-4540-AE66-6489F189FADC"
REDIRECT_URI = "com.bosch.tt.dashtt.pointt://app/login"
SCOPE = (
    "openid email profile offline_access pointt.gateway.claiming "
    "pointt.gateway.removal pointt.gateway.list pointt.gateway.users "
    "pointt.gateway.resource.dashapp pointt.castt.flow.token-exchange "
    "bacon hcc.tariff.read"
)


class RedirectError(ValueError):
    """A browser URL that cannot complete this OAuth flow."""


def authorization_code(redirect, expected_state, expected_redirect):
    callback = re.search(r"com\.bosch\.tt\.dashtt\.pointt://[^\s'\"<>]+", redirect)
    address = callback.group(0) if callback else redirect.strip()
    if "…" in address:
        raise RedirectError("The copied callback was truncated; use Copy message in the Console")
    actual = urlsplit(address)
    expected = urlsplit(expected_redirect)
    if actual.scheme == "https" and actual.netloc == "singlekey-id.com" and actual.path.endswith("/redirection"):
        raise RedirectError("SingleKey's web redirection page is intermediate. Click Continue, then copy the com.bosch...:// request URL from your browser's Network tab.")
    if (actual.scheme, actual.netloc, actual.path) != (
        expected.scheme, expected.netloc, expected.path
    ):
        raise RedirectError("Expected the com.bosch.tt.dashtt.pointt://app/login redirect address")
    query = parse_qs(actual.query)
    if query.get("state") != [expected_state]:
        raise RedirectError("OAuth state mismatch; restart the script and use its new login URL")
    if "error" in query:
        raise RedirectError("Bosch login returned an error")
    if len(query.get("code", [])) != 1:
        raise RedirectError("Redirect address has no authorization code")
    return query["code"][0]


def request_json(url, *, token=None, data=None, user_agent=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if user_agent:
        headers["User-Agent"] = user_agent
    with urlopen(Request(url, data=data, headers=headers), timeout=15) as response:
        return json.load(response)


def read_authorization_code(state):
    while True:
        line = getpass("Paste the callback URL containing code= and state= (hidden): ")
        if "com.bosch.tt.dashtt.pointt://" in line:
            return authorization_code(line, state, REDIRECT_URI)
        print("No Bosch callback on that line; paste the Failed to launch callback URL.")


def login():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    params = {
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "response_type": "code",
        "prompt": "login",
        "scope": SCOPE,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "style_id": "tt_bsch",
        "state": state,
    }
    print("Open this URL in the browser where you signed in to SingleKey:\n")
    print("https://singlekey-id.com/auth/connect/authorize?" + urlencode(params))
    print("\nBefore clicking Continue, open browser Developer Tools > Network and enable Preserve log.")
    print("Copy the complete com.bosch.tt.dashtt.pointt:// request URL or Console error after the redirect fails.")
    print("Leave this program waiting below. If you see a root@...# shell prompt, run it again.")
    code = read_authorization_code(state)
    token_response = request_json(
        "https://singlekey-id.com/auth/connect/token",
        data=urlencode(
            {
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": verifier,
                "code": code,
            }
        ).encode(),
    )
    token = token_response.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("No access token returned")
    print("Authentication: OK")
    return token_response


def main():
    token = login()["access_token"]

    for name, url, agent in (
        (
            "HomeCom gateways",
            "https://pointt-api.bosch-thermotechnology.com/pointt-api/api/v1/gateways/",
            None,
        ),
        (
            "Bacon devices",
            "https://claiming.euc1.bacon.bosch-tt-cw.com/v1/users/self/devices",
            "DashApp/4.0.0 (Android-Release)",
        ),
    ):
        try:
            devices = request_json(url, token=token, user_agent=agent)
            if not isinstance(devices, list):
                raise ValueError("Unexpected device list format")
            print(f"{name}: {len(devices)}")
            if name == "HomeCom gateways":
                for number, device in enumerate(devices, 1):
                    kind = device.get("deviceType", "unknown") if isinstance(device, dict) else "unknown"
                    kind = kind if isinstance(kind, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", kind) else "unknown"
                    print(f"  {number}: type={kind}")
        except HTTPError as exc:
            print(f"{name}: HTTP {exc.code}")
        except (URLError, ValueError, TypeError) as exc:
            print(f"{name}: {type(exc).__name__}")


if __name__ == "__main__":
    try:
        main()
    except HTTPError as exc:
        print(f"Test failed: HTTP {exc.code}")
        raise SystemExit(1) from None
    except RedirectError as exc:
        print(f"Test failed: {exc}")
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"Test failed: {type(exc).__name__}")
        raise SystemExit(1) from None
