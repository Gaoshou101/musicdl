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
