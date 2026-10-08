from dataclasses import replace

import pytest

from musicdl.admin.health import SourceHealthStore
from musicdl.media.models import DownloadEvent


def terminal(request="r", *, source="primary", version="1", candidate="track",
             status="success", error=None, requested="flac", actual="flac"):
    return DownloadEvent(request, candidate, source, version, "download", status,
                         error_code=error, requested_quality=requested, actual_quality=actual)


def metrics(store, source="primary"):
    return next(row["metrics"] for row in store.snapshot([{"id": source}])["sources"] if row["id"] == source)


def recent_requests(store, source="primary"):
    return next(row["recent_requests"] for row in store.snapshot([{"id": source}])["sources"]
                if row["id"] == source)


def test_search_traffic_cannot_improve_download_reliability_or_displace_its_window():
    store = SourceHealthStore()
    for i in range(5):
        store.observe_event(terminal(str(i), status="failed", error="media_timeout"))
    before = metrics(store)
    for _ in range(100):
        store.observe_search("primary", "ok", count=0, source_version="1")
        store.observe_event(replace(terminal(), stage="refresh"))
        store.observe_event(replace(terminal(), stage="health", healthy=True))
    after = metrics(store)
    assert after["search"] == {"samples": 20, "successes": 20, "failures": 0, "rate": 1.0}
    assert after["download"] == before["download"]
    assert after["download"]["rate"] == 0.0
    assert after["ranking"] == {"eligible": True, "minimum_samples": 5, "score": -6}
    assert store.fallback_health("primary") is False


def test_small_sample_scores_are_neutral_and_distinct_requests_count():
    store = SourceHealthStore()
    for i in range(4):
        store.observe_event(terminal(str(i)))
    assert metrics(store)["download"]["rate"] == 1.0
    assert store.delivery_preference("primary") == 0
    assert store.delivery_health("primary") is True
    assert metrics(store)["ranking"]["eligible"] is False
    store.observe_event(terminal("4"))
    assert store.delivery_preference("primary") == 6
    assert store.delivery_preference("unobserved") == 0


def test_terminal_replacements_deduplicate_and_remove_a_false_quality_success():
    store = SourceHealthStore()
    store.observe_event(terminal(status="failed", error="download_cancelled"))
    assert metrics(store)["download"]["samples"] == 0
    store.observe_event(terminal(status="failed", error="media_timeout"))
    assert metrics(store)["download"] == {"samples": 1, "successes": 0, "failures": 1, "excluded": 0, "rate": 0.0}
    store.observe_event(terminal())
    assert metrics(store)["quality"]["fulfilled"] == 1
    store.observe_event(terminal(status="failed", error="download_failed"))
    assert metrics(store)["download"]["samples"] == 1
    assert metrics(store)["quality"]["samples"] == 0


@pytest.mark.parametrize("error", ["download_cancelled", "path_escape", "media_host_denied", "invalid_max_bytes"])
def test_cancellation_and_explicit_local_refusals_do_not_penalize_a_channel(error):
    store = SourceHealthStore()
    store.observe_event(terminal(status="failed", error=error))
    result = metrics(store)
    assert result["download"]["excluded"] == 1
    assert result["download"]["samples"] == 0 and result["download"]["rate"] is None
    assert store.delivery_preference("primary") == 0


def test_invalid_source_chunks_and_content_failures_still_count_as_failures():
    store = SourceHealthStore()
    for i, error in enumerate(("invalid_chunk", "incomplete_audio", "download_failed")):
        store.observe_event(terminal(str(i), status="failed", error=error))
    assert metrics(store)["download"]["failures"] == 3


def test_quality_uses_only_successful_lossless_requests_with_proven_answers():
    store = SourceHealthStore()
    for i, actual in enumerate(("flac", "alac", "320k", "mp3", "master", "atmos_plus", None)):
        store.observe_event(terminal(str(i), actual=actual))
    store.observe_event(terminal("lossy-request", requested="320k", actual="320k"))
    store.observe_event(terminal("failed", status="failed", actual="flac"))
    store.observe_event(replace(terminal("probe"), stage="quality_downgraded"))
    assert metrics(store)["quality"] == {
        "samples": 4, "fulfilled": 2, "downgraded": 2, "unknown": 3, "rate": .5}
    assert metrics(store)["download"]["samples"] == 9


def test_independent_quality_window_survives_unrelated_lossy_downloads():
    store = SourceHealthStore(window=2)
    store.observe_event(terminal("lossless"))
    for i in range(5):
        store.observe_event(terminal(str(i), requested="320k", actual="320k"))
    assert metrics(store)["download"]["samples"] == 2
    assert metrics(store)["quality"]["fulfilled"] == 1
    store.observe_event(terminal("lossless", status="failed"))
    assert metrics(store)["quality"]["samples"] == 0


def test_window_eviction_is_bounded_and_candidate_keys_are_distinct():
    store = SourceHealthStore(window=2)
    for item in ("a", "b", "c"):
        store.observe_event(terminal(candidate=item, actual="mp3"))
    assert metrics(store)["download"]["samples"] == 2
    assert metrics(store)["quality"]["downgraded"] == 2
    assert len(store._records["primary"]["delivery_outcomes"]) == 2


def test_version_replacement_resets_metrics_and_rejects_retired_script_events():
    store = SourceHealthStore()
    store.bind_source_version("primary", "1")
    for i in range(5):
        store.observe_event(terminal(str(i)))
    assert store.delivery_preference("primary") == 6
    store.bind_source_version("primary", "2")
    assert metrics(store)["download"]["samples"] == 0
    assert store.delivery_health("primary") is None
    store.observe_event(terminal("late", version="1"))
    store.observe_search("primary", "ok", source_version="1")
    assert metrics(store)["download"]["samples"] == metrics(store)["search"]["samples"] == 0
    store.observe_event(terminal("new", version="2"))
    assert metrics(store)["download"]["samples"] == 1
    assert store.delivery_preference("primary") == 0


def test_delivery_health_ignores_local_refusals_and_search_cannot_clear_failure():
    store = SourceHealthStore()
    store.observe_event(terminal("success"))
    store.observe_event(terminal("cancel", status="failed", error="download_cancelled"))
    assert store.delivery_health("primary") is True
    store.observe_event(terminal("failure", status="failed", error="media_timeout"))
    store.observe_search("primary", "ok", source_version="1")
    store.observe_event(replace(terminal("ping"), stage="health", healthy=True))
    assert store.delivery_health("primary") is False


def test_excluded_traffic_cannot_evict_failed_delivery_health_evidence():
    store = SourceHealthStore()
    store.observe_event(terminal("failure", status="failed", error="media_timeout"))
    store.observe_search("primary", "ok", source_version="1")
    for i in range(30):
        store.observe_event(terminal(str(i), status="failed", error="download_cancelled"))
    assert metrics(store)["download"]["samples"] == 0
    assert store.delivery_health("primary") is False
    store.observe_event(terminal("recovered"))
    assert store.delivery_health("primary") is True
    store.observe_event(replace(terminal("ping"), stage="health", healthy=False))
    assert store.delivery_health("primary") is False


def test_unobserved_metrics_are_unknown_and_legacy_preference_is_retained():
    store = SourceHealthStore()
    assert metrics(store)["scope"] == "process"
    assert metrics(store)["search"]["rate"] is None
    assert metrics(store)["download"]["rate"] is None
    store.observe_search("primary", "ok")
    assert store.preference("primary") == 10
    assert store.delivery_preference("primary") == 0


def test_new_catalogue_observations_do_not_change_legacy_preference_inputs():
    store = SourceHealthStore()
    store.observe_event(terminal(status="failed", error="media_timeout"))
    legacy_score = store.preference("primary")
    for _ in range(30):
        store.observe_catalogue("primary", "ok", source_version="1")
    assert metrics(store)["search"]["samples"] == 20
    assert store.preference("primary") == legacy_score
    assert store.fallback_health("primary") is False


def test_recent_requests_are_empty_until_observed_and_search_outcomes_keep_order_and_cap():
    store = SourceHealthStore()
    assert recent_requests(store) == []

    statuses = ["ok", "timeout", "invalid", "unrecognized_status"] * 3
    for status in statuses:
        store.observe_catalogue("primary", status, count=0, source_version="1")

    requests = recent_requests(store)
    assert len(requests) == 10
    assert [item["kind"] for item in requests] == ["search"] * 10
    assert [item["status"] for item in requests] == [
        "failure", "unknown", "success", "failure", "failure",
        "unknown", "success", "failure", "failure", "unknown",
    ]
    assert all(isinstance(item["timestamp"], (int, float)) for item in requests)
    assert [item["timestamp"] for item in requests] == sorted(item["timestamp"] for item in requests)
    # The visible history has its own cap and does not change metric windows.
    assert metrics(store)["search"]["samples"] == 12


def test_recent_download_replacements_deduplicate_and_exclusions_are_distinct():
    store = SourceHealthStore()
    store.observe_event(terminal("same", status="success"))
    store.observe_event(terminal("same", status="failed", error="media_timeout"))
    requests = recent_requests(store)
    assert len(requests) == 1
    assert {key: value for key, value in requests[0].items() if key != "timestamp"} == {
        "kind": "download", "status": "failure",
    }
    assert metrics(store)["download"]["failures"] == 1

    store.observe_event(terminal("same", status="failed", error="path_escape"))
    store.observe_event(terminal("cancelled", status="failed", error="download_cancelled"))
    requests = recent_requests(store)
    assert [item["status"] for item in requests] == ["excluded", "excluded"]
    assert metrics(store)["download"] == {
        "samples": 0, "successes": 0, "failures": 0, "excluded": 2, "rate": None,
    }


def test_recent_history_resets_for_a_new_source_version_and_ignores_retired_events():
    store = SourceHealthStore()
    store.bind_source_version("primary", "1")
    store.observe_search("primary", "ok", source_version="1")
    store.observe_event(terminal("old", version="1"))
    store.bind_source_version("primary", "2")
    store.observe_search("primary", "timeout", source_version="1")
    store.observe_event(terminal("late", version="1"))
    assert recent_requests(store) == []

    store.observe_search("primary", "ok", source_version="2")
    assert [item["status"] for item in recent_requests(store)] == ["success"]
