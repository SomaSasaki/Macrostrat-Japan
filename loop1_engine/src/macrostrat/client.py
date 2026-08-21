"""Macrostrat v2 REST API Client.

Provides structured access to Macrostrat geological database endpoints:
- Units (stratigraphic units)
- Intervals (chronostratigraphic definitions)
- Lithologies (rock types & attributes)
"""

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class MacrostratClient:
    """Client for querying Macrostrat API v2."""

    BASE_URL: str = "https://macrostrat.org/api/v2"

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Internal helper to execute GET requests against Macrostrat API."""
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        if params:
            query_str = urllib.parse.urlencode(params)
            url = f"{url}?{query_str}"

        req = urllib.request.Request(url, headers={"User-Agent": "MacrostratJapan/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("success", {}).get("data", [])

    def get_units(
        self,
        interval_name: Optional[str] = None,
        strat_name: Optional[str] = None,
        project_id: Optional[int] = 1,
        response: str = "long",
    ) -> List[Dict[str, Any]]:
        """Fetch stratigraphic units with optional filters."""
        params: Dict[str, Any] = {"response": response}
        if interval_name:
            params["interval_name"] = interval_name
        if strat_name:
            params["strat_name"] = strat_name
        if project_id is not None:
            params["project_id"] = project_id
        return self._get("units", params=params)

    def get_intervals(
        self,
        interval_name: Optional[str] = None,
        rule: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch chronostratigraphic interval definitions."""
        params: Dict[str, Any] = {}
        if interval_name:
            params["interval_name"] = interval_name
        if rule:
            params["rule"] = rule
        return self._get("defs/intervals", params=params)

    def get_lithologies(
        self,
        lith_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch lithology definitions."""
        params: Dict[str, Any] = {}
        if lith_type:
            params["lith_type"] = lith_type
        return self._get("defs/lithologies", params=params)