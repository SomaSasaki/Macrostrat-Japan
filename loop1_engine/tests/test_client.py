"""Tests for MacrostratClient (Loop 1 Automated Testing)."""

import json
from unittest.mock import patch, MagicMock
import pytest
from macrostrat.client import MacrostratClient


@pytest.fixture
def client() -> MacrostratClient:
    return MacrostratClient(timeout=5.0)


def test_client_init(client: MacrostratClient) -> None:
    """Verify client initialization."""
    assert client.BASE_URL == "https://macrostrat.org/api/v2"
    assert client.timeout == 5.0


@patch("urllib.request.urlopen")
def test_get_units_mocked(mock_urlopen: MagicMock, client: MacrostratClient) -> None:
    """Verify units query constructs the expected parameters and returns data."""
    mock_response = MagicMock()
    payload = {
        "success": {
            "data": [
                {"unit_id": 1, "unit_name": "Morrison Formation", "max_thick": 100.0}
            ]
        }
    }
    mock_response.read.return_value = json.dumps(payload).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    units = client.get_units(interval_name="Jurassic", strat_name="Morrison")

    assert len(units) == 1
    assert units[0]["unit_name"] == "Morrison Formation"
    assert units[0]["unit_id"] == 1


@patch("urllib.request.urlopen")
def test_get_intervals_mocked(mock_urlopen: MagicMock, client: MacrostratClient) -> None:
    """Verify interval definitions query."""
    mock_response = MagicMock()
    payload = {
        "success": {
            "data": [
                {"int_id": 10, "name": "Cambrian", "b_age": 541.0, "t_age": 485.4}
            ]
        }
    }
    mock_response.read.return_value = json.dumps(payload).encode("utf-8")
    mock_response.__enter__.return_value = mock_response
    mock_urlopen.return_value = mock_response

    intervals = client.get_intervals(interval_name="Cambrian")

    assert len(intervals) == 1
    assert intervals[0]["name"] == "Cambrian"
    assert intervals[0]["b_age"] == 541.0