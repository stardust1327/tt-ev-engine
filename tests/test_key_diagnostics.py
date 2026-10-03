"""When The Odds API rejects the key, the error says what the saved key looks like."""

import pytest
import responses

from ev_engine.http_client import ApiError
from ev_engine.models import RunReport
from ev_engine.providers.the_odds_api import TheOddsApiProvider, describe_key

from conftest import NOW, make_settings

URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"


@pytest.mark.parametrize("key,expected", [
    ("a1b2c3d4e5f6", "12 characters, letters and digits only"),
    ("apiKey=a1b2c3", "contains 'apiKey='"),
    ("https://api.the-odds-api.com/v4/sports?apiKey=abc", "looks like part of a URL"),
    ('"a1b2c3"', "contains quote marks"),
    ("a1b2 c3d4", "contains spaces"),
    ("a1b2-c3d4", "characters other than letters and digits"),
])
def test_describe_key(key, expected):
    assert expected in describe_key(key)
    assert key not in describe_key(key) or len(key) < 3


@responses.activate
def test_invalid_key_error_describes_the_saved_key(tmp_path):
    responses.add(responses.GET, URL, status=401,
                  json={"message": "API key is not valid.", "error_code": "INVALID_KEY"})
    settings = make_settings(tmp_path, providers=("the_odds_api",), the_odds_api_key="apiKey=deadbeef1234",
                             odds_api_sports=("baseball_mlb",))
    provider = TheOddsApiProvider.from_settings(settings, RunReport(started_at=NOW), sleep=lambda _s: None)
    with pytest.raises(ApiError) as err:
        provider.fetch_events(NOW)
    message = str(err.value)
    assert "INVALID_KEY" in message and "saved THE_ODDS_API_KEY: 20 characters" in message
    assert "contains 'apiKey='" in message
    assert "deadbeef1234" not in message
