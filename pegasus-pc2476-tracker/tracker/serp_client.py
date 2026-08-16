"""
Thin wrapper around the SerpApi google_flights engine.

Deliberately minimal: one function, one HTTP request, no retries, no
follow-up calls to "double check" anything. Retry/backoff logic is
exactly the kind of thing that would silently double the monthly
request count, which this project cannot afford (see requirement #9
and #10 in the project brief).

Uses the `serpapi` package (pip install serpapi -> `import serpapi`),
which is SerpApi's current official Python client
(https://github.com/serpapi/serpapi-python). Falls back to a plain
`requests.get` if the package isn't installed, so the tracker still
works with zero extra dependencies beyond `requests`.
"""
from __future__ import annotations

import os

import requests

SERPAPI_ENDPOINT = "https://serpapi.com/search"


class SerpApiCallError(Exception):
    """Raised for network-level failures (no confirmed response from
    SerpApi at all) - distinct from SerpApi returning a JSON error body,
    which the extractor treats as INVALID_RESPONSE/API_ERROR instead.
    """


def run_search(search_parameters: dict, api_key: str | None = None) -> dict:
    """Performs exactly ONE SerpApi google_flights search and returns the
    parsed JSON response as a plain dict.
    """
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        raise SerpApiCallError(
            "SERPAPI_API_KEY is not set (expected as an environment "
            "variable / GitHub Actions secret)."
        )

    try:
        import serpapi  # official client: pip install serpapi
    except ImportError:
        serpapi = None

    if serpapi is not None:
        # Using the official client: this makes exactly one HTTP request
        # under the hood. If it raises, we surface that directly rather
        # than silently falling back to a second, raw HTTP attempt -
        # falling back here would risk two billed searches for one
        # scheduled execution.
        try:
            client = serpapi.Client(api_key=api_key)
            results = client.search(search_parameters)
        except Exception as exc:
            raise SerpApiCallError(f"serpapi client call failed: {exc}") from exc
        # SerpResults behaves like a dict but isn't guaranteed to BE one;
        # normalize so downstream code (and json.dump for raw evidence)
        # only ever deals with plain dict/list/str/number/bool/None.
        return dict(results)

    # serpapi package not installed - single plain HTTPS GET instead.
    params = dict(search_parameters)
    params["api_key"] = api_key
    try:
        response = requests.get(SERPAPI_ENDPOINT, params=params, timeout=30)
    except requests.RequestException as exc:
        raise SerpApiCallError(f"network error contacting SerpApi: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise SerpApiCallError(
            f"SerpApi response was not valid JSON (HTTP {response.status_code})"
        ) from exc
