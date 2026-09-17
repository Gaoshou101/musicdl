import asyncio

from musicdl.admin.health import EventLogStore, HealthAggregator
from musicdl.admin.portal import render_dashboard
from musicdl.media.models import DownloadEvent


def test_health_aggregator_degrades_failed_probe_without_failing_all():
    async def redis():
        return True

    async def plugin():
        raise RuntimeError("private details")

    report = asyncio.run(HealthAggregator({"readyz": redis, "redis": redis, "plugin_runner": plugin, "telegram": redis}).check())
    assert report["status"] == "degraded"
    assert report["checks"] == {"readyz": "ok", "redis": "ok", "plugin_runner": "unavailable", "telegram": "ok"}


def test_health_aggregator_reads_an_unused_dependency_as_not_required():
    """A dependency this deployment never calls must not colour the whole card."""

    async def ok():
        return True

    async def unused():
        return None

    report = asyncio.run(HealthAggregator({"readyz": ok, "redis": unused, "plugin_runner": ok,
                                           "telegram": unused}).check())
    assert report["status"] == "ok"
    assert report["checks"] == {"readyz": "ok", "redis": "not_required", "plugin_runner": "ok",
                                "telegram": "not_required"}


def test_health_aggregator_separates_a_missing_probe_from_an_unused_one():
    async def ok():
        return True

    report = asyncio.run(HealthAggregator({"readyz": ok, "redis": ok}).check())
    assert report["status"] == "degraded"
    assert report["checks"]["plugin_runner"] == "unavailable"
    assert report["checks"]["telegram"] == "unavailable"


def test_event_log_redacts_and_paginates():
    store = EventLogStore()
    store.append(DownloadEvent("req", "candidate", "source", "v1", "download", "failed", error_code="https://x/?token=secret"))
    page = store.page(offset=0, limit=1)
    assert page["total"] == 1
    assert "secret" not in str(page)
    assert page["items"][0]["error_code"] == "https://x/"


def test_dashboard_renderer_has_safe_fallback():
    html = render_dashboard(template_path="missing-dashboard-template.html", health="readyz: ok", sources="&lt;x&gt;", bots="bot", event_total=1, recent="safe", audit_total=1, audit_recent="audit")
    assert "readyz: ok" in html and "events" in html and "audit" in html
