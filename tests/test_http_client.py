"""ApiClient error reporting: the provider's own reason is surfaced in the error."""

import pytest
import responses

from ev_engine.http_client import ApiClient, ApiError, RateLimitPolicy

URL = "https://api.the-odds-api.com"


def client():
    return ApiClient("The Odds API", URL, timeout=5, policy=RateLimitPolicy(max_retries=0), sleep=lambda _s: None)


@responses.activate
@pytest.mark.parametrize("code", ["INVALID_KEY", "OUT_OF_USAGE_CREDITS", "DEACTIVATED_KEY"])
def test_401_includes_the_odds_api_error_code(code):
    responses.add(responses.GET, f"{URL}/v4/sports/baseball_mlb/odds", status=401,
                  json={"message": "explanation from the API", "error_code": code})
    with pytest.raises(ApiError) as err:
        client().get_json("/v4/sports/baseball_mlb/odds", {"apiKey": "k"})
    assert err.value.fatal and err.value.status == 401
    assert f"HTTP 401 ({code}: explanation from the API)" in str(err.value)


@responses.activate
def test_non_json_error_body_is_handled():
    responses.add(responses.GET, f"{URL}/v4/sports/x/odds", status=422, body="<html>nope</html>")
    with pytest.raises(ApiError) as err:
        client().get_json("/v4/sports/x/odds")
    assert str(err.value) == "The Odds API /v4/sports/x/odds: HTTP 422"
