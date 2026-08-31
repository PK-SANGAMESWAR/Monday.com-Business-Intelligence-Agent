"""Cases 13-29: transport, failure classification, retry and backoff.

Every case here is written against `respx`, so the suite runs offline (NFR-3).
The through-line: monday.com has an awkward habit of returning **HTTP 200 with an
`errors[]` array**, so a client that classifies on status code alone will treat an
authentication failure as a successful response and hand `None` to the layer
above. Half of these cases exist to prove that does not happen.

The other half prove *degradation* rather than raising: which failures are retried
and which are not. Retrying a bad token is a waste of a user's time; not retrying
a 503 loses an answer we could have given.
"""

from __future__ import annotations

import io
import logging
from typing import Any

import httpx
import pytest

from bi_agent.errors import (
    MondayAuthError,
    MondayError,
    MondayQueryError,
    MondayRateLimitError,
    MondayUnavailableError,
)
from bi_agent.logging_config import REDACTION_PLACEHOLDER, configure_logging
from bi_agent.monday.client import MondayClient, classify_failure
from bi_agent.monday.queries import BOARD_ITEMS_FIRST, ME
from tests.conftest import FAKE_MONDAY_TOKEN


def ok(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=payload)


# --- case 13: the happy path --------------------------------------------------


def test_successful_request_returns_data_and_sends_correct_headers(
    monday_client_factory, load_fixture
):
    """Case 13: `data` is unwrapped, and auth/version headers are right."""
    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("me")))

    data = client.execute(ME)

    assert data == {"me": {"id": "83990706", "name": "Test Founder", "is_admin": True}}
    assert route.call_count == 1

    request = route.calls.last.request
    assert request.headers["Authorization"] == FAKE_MONDAY_TOKEN
    assert request.headers["API-Version"] == "2024-10"
    assert request.headers["Content-Type"] == "application/json"


def test_request_body_carries_the_document_and_variables(
    monday_client_factory, load_fixture
):
    import json

    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("board_items_page1")))

    client.execute(BOARD_ITEMS_FIRST, {"boardIds": ["9876543210"], "limit": 500})

    body = json.loads(route.calls.last.request.content)
    assert body["query"] == BOARD_ITEMS_FIRST.text
    assert body["variables"] == {"boardIds": ["9876543210"], "limit": 500}


def test_api_version_is_pinned_from_settings(monday_client_factory, load_fixture):
    """Pinned so a server-side default change cannot alter response shapes."""
    client, route = monday_client_factory(monday_api_version="2025-01")
    route.mock(return_value=ok(load_fixture("me")))

    client.execute(ME)

    assert route.calls.last.request.headers["API-Version"] == "2025-01"


# --- case 14: the token never reaches the log stream --------------------------


def test_token_never_appears_in_logs_at_debug_level(
    monday_client_factory, load_fixture
):
    """Case 14: a full request cycle at DEBUG, with the token absent throughout."""
    stream = io.StringIO()
    configure_logging("DEBUG", secrets=[FAKE_MONDAY_TOKEN], stream=stream)
    try:
        client, route = monday_client_factory()
        route.mock(return_value=ok(load_fixture("me")))

        client.execute(ME)

        logged = stream.getvalue()
        assert logged, "the client must log something at DEBUG, or this proves nothing"
        assert FAKE_MONDAY_TOKEN not in logged
    finally:
        logging.getLogger().handlers.clear()


# --- cases 15-16: authentication ----------------------------------------------


def test_http_401_raises_auth_error_without_retrying(
    monday_client_factory, load_fixture
):
    """Case 15: retrying a rejected token wastes time and says nothing new."""
    client, route = monday_client_factory(max_retries=3)
    route.mock(return_value=httpx.Response(401, json=load_fixture("error_auth_401")))

    with pytest.raises(MondayAuthError):
        client.execute(ME)

    assert route.call_count == 1


def test_200_with_authentication_error_in_errors_array_raises_auth_error(
    monday_client_factory, load_fixture
):
    """Case 16: the 200-with-errors case, which a status-only classifier misses."""
    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("error_auth")))

    with pytest.raises(MondayAuthError):
        client.execute(ME)

    assert route.call_count == 1


def test_auth_error_user_message_names_the_problem_without_the_token(
    monday_client_factory, load_fixture
):
    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("error_auth")))

    with pytest.raises(MondayAuthError) as excinfo:
        client.execute(ME)

    assert "authenticate" in excinfo.value.user_message
    assert FAKE_MONDAY_TOKEN not in str(excinfo.value)
    assert FAKE_MONDAY_TOKEN not in excinfo.value.user_message


# --- cases 17-18: rate limiting -----------------------------------------------


def test_429_with_retry_after_header_is_honoured(
    monday_client_factory, load_fixture, recorded_sleep
):
    """Case 17: when the API says how long to wait, waiting longer is rude and
    waiting less is pointless."""
    client, route = monday_client_factory(max_retries=1)
    route.mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "5"}, json=load_fixture("error_rate_limit")
        )
    )

    with pytest.raises(MondayRateLimitError) as excinfo:
        client.execute(ME)

    assert excinfo.value.retry_after == 5
    assert recorded_sleep.delays == [5.0]


def test_rate_limit_then_success(monday_client_factory, load_fixture, caplog):
    """Case 18: the retry succeeds, and the retry is visible in the log."""
    client, route = monday_client_factory()
    route.mock(
        side_effect=[
            httpx.Response(429, json=load_fixture("error_rate_limit")),
            ok(load_fixture("me")),
        ]
    )

    with caplog.at_level(logging.WARNING):
        data = client.execute(ME)

    assert data["me"]["id"] == "83990706"
    assert route.call_count == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_complexity_budget_message_is_treated_as_a_rate_limit(
    monday_client_factory, load_fixture
):
    """monday.com meters by complexity, not request count - and reports it as a
    200 with an error message, not a 429."""
    client, route = monday_client_factory(max_retries=0)
    route.mock(return_value=ok(load_fixture("error_complexity")))

    with pytest.raises(MondayRateLimitError) as excinfo:
        client.execute(BOARD_ITEMS_FIRST, {"boardIds": ["1"], "limit": 500})

    # "reset in 37 seconds" is the only wait hint we get on this path.
    assert excinfo.value.retry_after == 37


# --- cases 19-21: server errors and the retry budget --------------------------


def test_three_server_errors_then_success(monday_client_factory, load_fixture):
    """Case 19: exactly 3 retries at the default budget, then the answer."""
    client, route = monday_client_factory(max_retries=3)
    route.mock(
        side_effect=[
            httpx.Response(500, json=load_fixture("server_error")),
            httpx.Response(502, json=load_fixture("server_error")),
            httpx.Response(503, json=load_fixture("server_error")),
            ok(load_fixture("me")),
        ]
    )

    data = client.execute(ME)

    assert data["me"]["id"] == "83990706"
    assert route.call_count == 4


def test_persistent_server_error_gives_up_after_max_retries(
    monday_client_factory, load_fixture, recorded_sleep
):
    """Case 20: it terminates. An infinite retry loop is a hung UI."""
    client, route = monday_client_factory(max_retries=3)
    route.mock(return_value=httpx.Response(500, json=load_fixture("server_error")))

    with pytest.raises(MondayUnavailableError):
        client.execute(ME)

    assert route.call_count == 4  # 1 attempt + 3 retries
    assert len(recorded_sleep) == 3


def test_max_retries_zero_is_honoured(monday_client_factory, load_fixture, recorded_sleep):
    """Case 21: the setting means what it says."""
    client, route = monday_client_factory(max_retries=0)
    route.mock(return_value=httpx.Response(503, json=load_fixture("server_error")))

    with pytest.raises(MondayUnavailableError):
        client.execute(ME)

    assert route.call_count == 1
    assert len(recorded_sleep) == 0


# --- cases 22-23: the network itself ------------------------------------------


def test_timeout_is_unavailable_and_is_retried(monday_client_factory, load_fixture):
    """Case 22: a timeout is the API being slow, not our query being wrong."""
    client, route = monday_client_factory(max_retries=2)
    route.mock(
        side_effect=[
            httpx.TimeoutException("timed out"),
            httpx.TimeoutException("timed out"),
            ok(load_fixture("me")),
        ]
    )

    data = client.execute(ME)

    assert data["me"]["id"] == "83990706"
    assert route.call_count == 3


def test_connection_error_is_unavailable_and_is_retried(monday_client_factory):
    """Case 23: DNS failure, refused connection, dropped Wi-Fi."""
    client, route = monday_client_factory(max_retries=2)
    route.mock(side_effect=httpx.ConnectError("name resolution failed"))

    with pytest.raises(MondayUnavailableError):
        client.execute(ME)

    assert route.call_count == 3


def test_network_failures_do_not_leak_the_url_with_credentials(monday_client_factory):
    client, route = monday_client_factory(max_retries=0)
    route.mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(MondayUnavailableError) as excinfo:
        client.execute(ME)

    assert FAKE_MONDAY_TOKEN not in str(excinfo.value)


# --- cases 24-26: malformed and rejected responses ----------------------------


def test_200_with_non_auth_errors_raises_query_error_and_is_not_retried(
    monday_client_factory, load_fixture
):
    """Case 24: a malformed query fails identically on retry, so do not retry."""
    client, route = monday_client_factory(max_retries=3)
    route.mock(return_value=ok(load_fixture("error_query")))

    with pytest.raises(MondayQueryError) as excinfo:
        client.execute(ME)

    assert route.call_count == 1
    assert "nonsense" in str(excinfo.value)


def test_200_without_a_data_key_raises_query_error_not_key_error(
    monday_client_factory, load_fixture
):
    """Case 25: a `KeyError` here would surface to a founder as a crash."""
    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("error_no_data_key")))

    with pytest.raises(MondayQueryError):
        client.execute(ME)


def test_200_with_data_that_is_not_a_dict_raises_query_error(
    monday_client_factory, load_fixture
):
    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("error_data_not_a_dict")))

    with pytest.raises(MondayQueryError):
        client.execute(ME)


def test_200_with_invalid_json_raises_query_error_not_a_json_traceback(
    monday_client_factory,
):
    """Case 26: an HTML error page from a proxy is a plausible real body."""
    client, route = monday_client_factory()
    route.mock(
        return_value=httpx.Response(
            200, content=b"<html>502 Bad Gateway</html>", headers={"Content-Type": "text/html"}
        )
    )

    with pytest.raises(MondayQueryError) as excinfo:
        client.execute(ME)

    assert isinstance(excinfo.value, MondayError)


# --- case 27: backoff timing --------------------------------------------------


def test_backoff_delays_grow_exponentially(
    monday_client_factory, load_fixture, recorded_sleep
):
    """Case 27: exponential growth, asserted in microseconds via injected sleep.

    The factory injects a jitter of 1.0, so each recorded delay is the full
    bound; the randomness is exercised in the next test instead.
    """
    client, route = monday_client_factory(max_retries=3)
    route.mock(return_value=httpx.Response(500, json=load_fixture("server_error")))

    with pytest.raises(MondayUnavailableError):
        client.execute(ME)

    assert recorded_sleep.delays == [1.0, 2.0, 4.0]


def test_backoff_is_jittered(monday_client_factory, load_fixture, recorded_sleep):
    """Full jitter: the delay is a draw from [0, bound], not the bound itself.

    Without it, every client retrying a recovering server retries in lockstep and
    knocks it over again.
    """
    draws = iter([0.25, 0.5, 0.75])
    client, route = monday_client_factory(max_retries=3, jitter=lambda: next(draws))
    route.mock(return_value=httpx.Response(500, json=load_fixture("server_error")))

    with pytest.raises(MondayUnavailableError):
        client.execute(ME)

    assert recorded_sleep.delays == [0.25, 1.0, 3.0]


def test_backoff_is_capped(monday_client_factory, load_fixture, recorded_sleep):
    """Unbounded doubling would eventually park a user behind a 17-minute wait."""
    client, route = monday_client_factory(max_retries=10)
    route.mock(return_value=httpx.Response(500, json=load_fixture("server_error")))

    with pytest.raises(MondayUnavailableError):
        client.execute(ME)

    assert max(recorded_sleep.delays) <= MondayClient.BACKOFF_CAP_SECONDS
    assert recorded_sleep.delays[-1] == MondayClient.BACKOFF_CAP_SECONDS


def test_real_jitter_is_random_by_default(settings_factory):
    """The default jitter is a real draw, not the deterministic test double."""
    client = MondayClient(settings_factory())
    try:
        draws = {client._jitter() for _ in range(20)}
    finally:
        client.close()

    assert len(draws) > 1
    assert all(0.0 <= draw <= 1.0 for draw in draws)


# --- case 28: error bodies that echo request context --------------------------


def test_error_body_echoing_the_token_is_redacted_in_logs(
    monday_client_factory, load_fixture
):
    """Case 28: the leak path F01's filter was written for, exercised end to end.

    monday.com echoes request context in some error payloads. If the token is in
    that payload and we log the payload, the token is in the log — unless the
    redaction filter is actually engaged on this code path.
    """
    payload = load_fixture("error_query_echoing_request")
    payload["errors"][0]["message"] = payload["errors"][0]["message"].replace(
        "TOKEN_PLACEHOLDER", FAKE_MONDAY_TOKEN
    )

    stream = io.StringIO()
    configure_logging("DEBUG", secrets=[FAKE_MONDAY_TOKEN], stream=stream)
    try:
        client, route = monday_client_factory()
        route.mock(return_value=ok(payload))

        with pytest.raises(MondayQueryError):
            client.execute(ME)

        logged = stream.getvalue()
        assert "Invalid request" in logged, "the failing body must be logged at all"
        assert FAKE_MONDAY_TOKEN not in logged
        assert REDACTION_PLACEHOLDER in logged
    finally:
        logging.getLogger().handlers.clear()


# --- case 29: connection lifecycle --------------------------------------------


def test_client_closes_its_connection_pool(settings_factory):
    """Case 29: no leaked transport. Streamlit reruns the script constantly."""
    client = MondayClient(settings_factory())
    assert client.is_closed is False

    client.close()

    assert client.is_closed is True


def test_client_works_as_a_context_manager(settings_factory, respx_mock, load_fixture):
    respx_mock.post("https://api.monday.com/v2").mock(
        return_value=ok(load_fixture("me"))
    )

    with MondayClient(settings_factory()) as client:
        assert client.execute(ME)["me"]["id"] == "83990706"

    assert client.is_closed is True


def test_close_is_idempotent(settings_factory):
    client = MondayClient(settings_factory())
    client.close()
    client.close()
    assert client.is_closed is True


# --- observability ------------------------------------------------------------


def test_complexity_spend_is_logged(monday_client_factory, load_fixture, caplog):
    """NFR-7: spend is observable per request, not inferred from a bill."""
    client, route = monday_client_factory()
    route.mock(return_value=ok(load_fixture("board_items_page1")))

    with caplog.at_level(logging.DEBUG):
        client.execute(BOARD_ITEMS_FIRST, {"boardIds": ["9876543210"], "limit": 500})

    assert any("989970" in record.getMessage() for record in caplog.records)


def test_every_log_record_carries_a_request_id(
    monday_client_factory, load_fixture, caplog
):
    """One correlation id per request, so a retry storm is readable afterwards."""
    client, route = monday_client_factory(max_retries=1)
    route.mock(
        side_effect=[
            httpx.Response(500, json=load_fixture("server_error")),
            ok(load_fixture("me")),
        ]
    )

    with caplog.at_level(logging.DEBUG):
        client.execute(ME)

    ids = {
        getattr(record, "request_id", None)
        for record in caplog.records
        if record.name.startswith("bi_agent")
    }
    assert len(ids) == 1
    assert ids != {None}


# --- classification edge cases -----------------------------------------------
#
# `classify_failure` is a pure function of (status, body), which is what lets
# these cases be written directly. That matters because the doc flags this table
# as the part most likely to need correcting against the live API: a correction
# here should be provable without a transport.


def make_response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, json={})


def test_classify_tolerates_a_body_that_is_not_a_dict():
    """A JSON array is valid JSON and an invalid response. Do not crash on it."""
    failure = classify_failure(make_response(200), [], document_name="ME")
    assert isinstance(failure, MondayQueryError)


def test_classify_reads_errors_given_as_plain_strings():
    """Not the documented shape, but cheap to survive and expensive to miss."""
    failure = classify_failure(
        make_response(200), {"errors": ["Not Authenticated"]}, document_name="ME"
    )
    assert isinstance(failure, MondayAuthError)


def test_classify_reads_the_top_level_error_message_shape():
    failure = classify_failure(
        make_response(400),
        {"error_message": "Rate Limit Exceeded", "status_code": 400},
        document_name="ME",
    )
    assert isinstance(failure, MondayRateLimitError)


def test_classify_treats_an_unexpected_status_with_no_errors_as_a_query_error():
    """A 404 with an empty body: not auth, not throttling, not a 5xx."""
    failure = classify_failure(make_response(404), {}, document_name="ME")
    assert isinstance(failure, MondayQueryError)
    assert "404" in str(failure)


def test_classify_returns_none_for_a_genuine_success():
    assert (
        classify_failure(
            make_response(200), {"data": {"me": {"id": "1"}}}, document_name="ME"
        )
        is None
    )


def test_http_date_retry_after_falls_back_to_our_own_backoff(
    monday_client_factory, load_fixture, recorded_sleep
):
    """An HTTP-date `Retry-After` is legal. Mis-parsing it as seconds would mean
    either hammering the API or sleeping for a decade, so we ignore it and use
    exponential backoff instead."""
    client, route = monday_client_factory(max_retries=1)
    route.mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
            json=load_fixture("error_rate_limit"),
        )
    )

    with pytest.raises(MondayRateLimitError) as excinfo:
        client.execute(ME)

    assert excinfo.value.retry_after is None
    assert recorded_sleep.delays == [1.0]


def test_zero_retry_after_does_not_disable_backoff(monday_client_factory, load_fixture):
    """`Retry-After: 0` must not become "retry instantly, forever"."""
    client, route = monday_client_factory(max_retries=1)
    route.mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "0"}, json=load_fixture("error_rate_limit")
        )
    )

    with pytest.raises(MondayRateLimitError):
        client.execute(ME)


def test_an_injected_http_client_is_not_closed_by_us(settings_factory):
    """A caller who supplied the pool owns the pool."""
    external = httpx.Client()
    try:
        client = MondayClient(settings_factory(), http_client=external)
        client.close()
        assert external.is_closed is False
    finally:
        external.close()


def test_classify_reads_every_error_in_the_array():
    """A multi-field failure returns several errors; all of them reach the log.

    Reporting only the first turns "three columns are wrong" into three separate
    debugging rounds.
    """
    failure = classify_failure(
        make_response(200),
        {
            "errors": [
                {"message": "Field 'a' doesn't exist on type 'Board'"},
                {"message": "Field 'b' doesn't exist on type 'Board'"},
                {"not_a_message": "ignored"},
            ]
        },
        document_name="BOARD_ITEMS_FIRST",
    )
    assert isinstance(failure, MondayQueryError)
    assert "Field 'a'" in str(failure) and "Field 'b'" in str(failure)


def test_reset_hint_is_found_in_a_later_error_message():
    """The wait hint need not be in the first error, and need not be alone."""
    failure = classify_failure(
        make_response(200),
        {
            "errors": [
                "Complexity budget exhausted",
                {"message": "budget remaining 0, reset in 12 seconds. Try later."},
            ]
        },
        document_name="BOARD_ITEMS_FIRST",
    )
    assert isinstance(failure, MondayRateLimitError)
    assert failure.retry_after == 12
