"""The per-source lossless self-check: one resolve, one verdict, one cache.

A channel either hands over a lossless file for a song it lists or it does not,
and only a real resolve says which. What this process measured becomes a
preference for the search that picks a channel, never a claim written onto a
candidate: the answer is evidence, not metadata. So the check asks one question,
judges it from the container the source declared and never from the shape of a
URL, and lets every failure -- a timeout, an exception, an answer with no
container -- stay ``unknown`` instead of quietly becoming "incapable".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from musicdl.admin.health import SourceHealthStore
from musicdl.sources.lossless_probe import PROBE_QUALITY, LosslessProbe


def run(coro):
    return asyncio.run(coro)


class Resolver:
    """A resolver that records every question it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, source_id, quality="flac"):
        self.calls.append((source_id, quality))
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise RuntimeError("no_answer")
        return answer


def test_one_check_asks_for_flac_and_records_the_container_evidence():
    """A lone container is the flac request echoed back, so it is not a proof."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="flac", quality=None, url="http://host/stream.php"))
    probe = LosslessProbe(resolver, store, ttl=1000.0, clock=lambda: 500.0)

    result = run(probe.check("primary", source_version="2"))

    assert resolver.calls == [("primary", PROBE_QUALITY)]
    assert result["status"] == "unknown"
    assert result["evidence"] == {"extension": "flac", "quality": None}
    assert result["checked_at"] == 500.0
    recorded = store.lossless_status("primary")
    assert recorded["status"] == "unknown"
    assert recorded["evidence"] == {"extension": "flac", "quality": None}
    assert store.lossless_capability("primary", source_version="2", now=500.0) is None


@pytest.mark.parametrize("tier", ["flac", "flac24bit"])
def test_a_genuine_flac_url_is_lossless(tier):
    """.flac in the path beside the matching named tier is a real answer."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="flac", quality=tier,
                                        url="http://host/song.flac"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "lossless"
    assert result["evidence"] == {"extension": "flac", "quality": tier}
    assert store.lossless_capability("primary", now=1.0) is True


def test_a_lossy_tier_beside_a_lossless_container_contradicts_it():
    """The measured defect: a flac container cannot outvote the source's own 320k."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="flac", quality="320k"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "lossy"
    assert result["evidence"] == {"extension": "flac", "quality": "320k"}
    assert store.lossless_capability("primary", now=1.0) is False

def test_a_lossless_label_cannot_survive_a_lossy_container():
    """A contract-valid ``mp3`` answer labelled flac is still an mp3 payload."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="mp3", quality="flac"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "lossy"
    assert result["evidence"] == {"extension": "mp3", "quality": "flac"}
    assert store.lossless_capability("primary", now=1.0) is False


def test_an_mp4_container_only_carries_the_lossless_tier_it_names():
    """An MP4 container is ALAC or AAC, so only ``alac`` proves it lossless."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="m4a", quality="flac"),
                        SimpleNamespace(extension="m4a", quality="alac"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    assert run(probe.check("named_wrong"))["status"] == "unknown"
    assert run(probe.check("named_right"))["status"] == "lossless"
    assert store.lossless_capability("named_wrong", now=1.0) is None
    assert store.lossless_capability("named_right", now=1.0) is True


def test_a_suffixless_url_whose_answer_names_a_lossless_tier_is_lossless():
    """The .php shape that really hands over flac: the URL never decides."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="flac", quality="flac",
                                        url="http://host/wy/wy.php?type=flac"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "lossless"
    assert result["evidence"] == {"extension": "flac", "quality": "flac"}


def test_a_url_suffix_alone_is_unknown_and_never_becomes_evidence():
    """.flac in a path is not the container; the descriptor is the evidence."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(url="http://host/song.flac"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "unknown"
    assert result["evidence"] == {"extension": None, "quality": None}
    assert "flac" not in str(result["evidence"])
    assert store.lossless_status("primary")["status"] == "unknown"
    assert store.lossless_capability("primary") is None


def test_a_timeout_stays_unknown_and_is_never_cached_as_lossy():
    store = SourceHealthStore()

    async def slow(source_id, quality="flac"):
        await asyncio.sleep(1.0)
        return SimpleNamespace(extension="mp3")

    probe = LosslessProbe(slow, store, timeout=0.01, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "unknown"
    assert store.lossless_status("primary")["status"] == "unknown"
    assert store.lossless_capability("primary") is None


def test_an_error_stays_unknown():
    store = SourceHealthStore()

    async def broken(source_id, quality="flac"):
        raise RuntimeError("upstream is down")

    probe = LosslessProbe(broken, store, clock=lambda: 1.0)

    assert run(probe.check("primary"))["status"] == "unknown"
    assert store.lossless_status("primary")["status"] == "unknown"
    assert store.lossless_capability("primary") is None


def test_concurrent_checks_for_one_source_share_a_single_probe():
    store = SourceHealthStore()
    calls = []

    async def slow(source_id, quality="flac"):
        calls.append((source_id, quality))
        await asyncio.sleep(0.02)
        return SimpleNamespace(extension="flac", quality="flac")

    async def scenario():
        probe = LosslessProbe(slow, store, clock=lambda: 1.0)
        return await asyncio.gather(probe.check("primary"), probe.check("primary"),
                                    probe.check("primary"))

    results = run(scenario())

    assert calls == [("primary", PROBE_QUALITY)]
    assert [item["status"] for item in results] == ["lossless"] * 3


def test_a_fresh_record_is_reused_without_a_second_resolve():
    store = SourceHealthStore()
    store.observe_lossless("primary", capable=True, evidence={"extension": "flac"}, checked_at=100.0)
    resolver = Resolver(SimpleNamespace(extension="mp3"))
    probe = LosslessProbe(resolver, store, ttl=1000.0, clock=lambda: 200.0)

    result = run(probe.check("primary"))

    assert resolver.calls == []
    assert result["status"] == "lossless" and result["checked_at"] == 100.0


def test_a_stale_record_forces_a_new_probe():
    store = SourceHealthStore()
    store.observe_lossless("primary", capable=True, checked_at=100.0)
    resolver = Resolver(SimpleNamespace(extension="mp3"))
    probe = LosslessProbe(resolver, store, ttl=10.0, clock=lambda: 200.0)

    result = run(probe.check("primary"))

    assert resolver.calls == [("primary", PROBE_QUALITY)]
    assert result["status"] == "lossy"


def test_a_changed_source_version_forces_a_new_probe():
    store = SourceHealthStore()
    store.observe_lossless("primary", capable=True, checked_at=100.0, source_version="1")
    resolver = Resolver(SimpleNamespace(extension="mp3"))
    probe = LosslessProbe(resolver, store, ttl=1000.0, clock=lambda: 200.0)

    result = run(probe.check("primary", source_version="2"))

    assert resolver.calls == [("primary", PROBE_QUALITY)]
    assert result["status"] == "lossy"


def test_the_actual_quality_field_outranks_the_declared_one():
    """An ALAC in an m4a container is lossless even when the tier label says 320k."""
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="m4a", actual_quality="alac", quality="320k"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary"))

    assert result["status"] == "lossless"
    assert result["evidence"] == {"extension": "m4a", "quality": "alac"}


def test_an_ambiguous_container_alone_is_unknown():
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="m4a"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    assert run(probe.check("primary"))["status"] == "unknown"


def test_a_declared_lossy_tier_is_a_lossy_verdict():
    store = SourceHealthStore()
    resolver = Resolver(SimpleNamespace(extension="mp3", quality="320k"))
    probe = LosslessProbe(resolver, store, clock=lambda: 1.0)

    result = run(probe.check("primary", source_version="9"))

    assert result["status"] == "lossy"
    assert store.lossless_capability("primary", source_version="9", now=1.0) is False


def test_a_plain_one_argument_resolver_is_accepted():
    """The pinned resolver form takes only a source id."""
    store = SourceHealthStore()
    seen = []

    async def resolve(source_id):
        seen.append(source_id)
        return SimpleNamespace(extension="flac", quality="flac")

    probe = LosslessProbe(resolve, store, clock=lambda: 1.0)

    assert run(probe.check("primary"))["status"] == "lossless"
    assert seen == ["primary"]


def test_a_plain_observer_callback_is_accepted_as_well_as_a_store():
    recorded = []

    def observe(source_id, *, capable, evidence, source_version, checked_at):
        recorded.append((source_id, capable, evidence, source_version, checked_at))

    probe = LosslessProbe(Resolver(SimpleNamespace(extension="mp3", quality="320k")), observe,
                          clock=lambda: 7.0)

    result = run(probe.check("primary", source_version="9"))

    assert result["status"] == "lossy"
    assert recorded == [("primary", False, {"extension": "mp3", "quality": "320k"}, "9", 7.0)]


def test_an_empty_source_id_is_refused():
    probe = LosslessProbe(Resolver(SimpleNamespace(extension="flac")), SourceHealthStore())
    with pytest.raises(ValueError):
        run(probe.check(""))
