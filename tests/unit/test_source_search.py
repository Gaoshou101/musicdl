import asyncio
import time
import pytest

from musicdl.sources.models import Candidate
from musicdl.sources.registry import SourceEntry, SourceRegistry
from musicdl.sources.search import search_sources


def candidate(source, item, title="Song"):
    return Candidate(source_id=source, source_version="1", item_id=item, title=title, artist="Artist", size=1024)


def test_sources_overlap_timeout_and_bad_source_isolated():
    started = []
    async def healthy(query):
        started.append("h")
        await asyncio.sleep(.03)
        return [candidate("h", "1")]
    async def slow(query):
        started.append("s")
        await asyncio.sleep(.5)
        return [candidate("s", "1")]
    registry = SourceRegistry([SourceEntry("h", "1", healthy, priority=1), SourceEntry("s", "1", slow, priority=2)])
    result = asyncio.run(search_sources(registry, "secret query", timeout=.2))
    assert [c.source_id for c in result.candidates] == ["h"]
    assert {s.source_id: s.status for s in result.statuses}["s"] == "timeout"
    assert started == ["h", "s"]


def test_dedup_sort_and_version_are_completion_order_independent():
    async def one(query):
        await asyncio.sleep(.02)
        return [candidate("a", "2", "Z"), candidate("a", "1", "A")]
    async def two(query):
        await asyncio.sleep(.001)
        return [candidate("b", "1", "B")]
    registry = SourceRegistry([SourceEntry("a", "1", one, priority=2), SourceEntry("b", "1", two, priority=1)])
    first = asyncio.run(search_sources(registry, "q"))
    second = asyncio.run(search_sources(registry, "q"))
    assert [(c.source_id, c.item_id) for c in first.candidates] == [("b", "1"), ("a", "1"), ("a", "2")]
    assert first.version == second.version


def test_mismatched_candidate_isolated_and_error_safe():
    async def bad(query):
        return [Candidate(source_id="other", source_version="1", item_id="1", title="x", artist="y")]
    registry = SourceRegistry([SourceEntry("a", "1", bad)])
    result = asyncio.run(search_sources(registry, "q"))
    assert result.candidates == ()
    assert result.statuses[0].status == "invalid"
    assert "query" not in result.statuses[0].as_log_fields()


def test_duplicate_identity_selects_richer_candidate():
    async def source(query):
        return [candidate("a", "1"), candidate("a", "1").model_copy(update={"size": 2048})]
    registry = SourceRegistry([SourceEntry("a", "1", source)])
    result = asyncio.run(search_sources(registry, "q"))
    assert len(result.candidates) == 1
    assert result.candidates[0].size == 2048


def test_cross_source_same_version_collapses_but_quality_versions_remain():
    async def first(query):
        return [Candidate(source_id="a", source_version="1", item_id="x", title="Song", artist="Artist", album="Album", duration=60, format="mp3", bitrate=128, size=1000)]
    async def second(query):
        return [Candidate(source_id="b", source_version="1", item_id="y", title=" Song ", artist="Artist", album="Album", duration=60, format="mp3", bitrate=128, size=2000), Candidate(source_id="b", source_version="1", item_id="z", title="Song", artist="Artist", album="Album", duration=60, format="flac", bitrate=900, size=3000)]
    result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", first, priority=2), SourceEntry("b", "1", second, priority=1)]), "song"))
    assert len(result.candidates) == 2
    assert result.candidates[0].source_id == "b"


def test_relevance_invalid_inputs_and_exception_safety():
    async def boom(query):
        raise RuntimeError("secret exception")
    registry = SourceRegistry([SourceEntry("a", "1", boom)])
    result = asyncio.run(search_sources(registry, "needle"))
    assert result.statuses[0].status == "error"
    assert "secret" not in str(result.statuses[0].as_log_fields())
    with pytest.raises(ValueError): asyncio.run(search_sources(registry, "   "))
    with pytest.raises(ValueError): asyncio.run(search_sources(registry, "x", timeout=0))


def test_exact_query_relevance_outranks_lexical_order():
    async def source(query):
        return [candidate("a", "1", "A needle extended"), candidate("a", "2", "Needle")]
    result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", source)]), "needle"))
    assert result.candidates[0].title == "Needle"


def test_reversed_completion_order_has_identical_candidates_version_and_statuses():
    async def slow_a(query):
        await asyncio.sleep(slow_a.delay)
        return [candidate("a", "1", "Alpha")]
    async def slow_b(query):
        await asyncio.sleep(slow_b.delay)
        return [candidate("b", "1", "Beta")]
    slow_a.delay, slow_b.delay = .04, .001
    registry = SourceRegistry([SourceEntry("a", "1", slow_a), SourceEntry("b", "1", slow_b)])
    first = asyncio.run(search_sources(registry, "x"))
    slow_a.delay, slow_b.delay = .001, .04
    second = asyncio.run(search_sources(registry, "x"))
    assert first.candidates == second.candidates
    assert first.version == second.version
    assert first.statuses == second.statuses


def test_priority_resolves_equal_relevance_and_version():
    async def first(query): return [Candidate(source_id="a", source_version="1", item_id="a", title="Same", artist="Artist", album="Album A", format="mp3", bitrate=128)]
    async def second(query): return [Candidate(source_id="b", source_version="1", item_id="b", title="Same", artist="Artist", album="Album B", format="mp3", bitrate=128)]
    result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", first, priority=5), SourceEntry("b", "1", second, priority=1)]), "same"))
    assert [c.source_id for c in result.candidates] == ["b", "a"]


def test_normalized_query_exact_match_and_material_digest_change():
    async def source(query): return [Candidate(source_id="a", source_version="1", item_id="1", title="Ａ  song", artist="Artist", size=100), Candidate(source_id="a", source_version="1", item_id="2", title="0 track", artist="A song singer", size=100)]
    registry = SourceRegistry([SourceEntry("a", "1", source)])
    first = asyncio.run(search_sources(registry, " Ａ\u3000song "))
    async def changed(query): return [Candidate(source_id="a", source_version="1", item_id="1", title="Ａ  song", artist="Artist", size=101)]
    second = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", changed)]), "a song"))
    assert [c.title for c in first.candidates] == ["A song", "0 track"]
    assert first.version != second.version

def test_quality_sort_is_explainable_and_not_title_lexical_order():
    async def source(query):
        return [Candidate(source_id="a", source_version="1", item_id="lossy", title="A", artist="Artist", format="mp3", bitrate=64), Candidate(source_id="a", source_version="1", item_id="lossless", title="Z", artist="Artist", format="flac", bitrate=1411)]
    result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", source)]), "artist"))
    assert [c.item_id for c in result.candidates] == ["lossless", "lossy"]

def test_quality_sort_prefers_higher_mp3_bitrate_over_lexical_title():
    async def source(query):
        return [Candidate(source_id="a", source_version="1", item_id="low", title="A", artist="Artist", format="mp3", bitrate=64), Candidate(source_id="a", source_version="1", item_id="high", title="Z", artist="Artist", format="mp3", bitrate=320)]
    result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", source)]), "artist"))
    assert [c.item_id for c in result.candidates] == ["high", "low"]

def test_source_result_boundaries_and_max_results_argument():
    async def generator(query):
        yield candidate("a", "1")
    async def too_many(query):
        return [candidate("a", str(i)) for i in range(101)]
    async def non_sequence(query):
        return "bad"
    for fn in (generator, too_many, non_sequence):
        result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", fn)]), "x"))
        assert result.statuses[0].status == "invalid"
    with pytest.raises(ValueError, match="invalid_max_results_per_source"):
        asyncio.run(search_sources(SourceRegistry(), "x", max_results_per_source=0))

def test_artist_title_query_relevance_outranks_compilation():
    c_exact = Candidate(source_id="a", source_version="1", item_id="1", title="晴天", artist="周杰伦")
    c_compilation = Candidate(source_id="a", source_version="1", item_id="2", title="周杰伦 晴天演唱会版", artist="群星")
    async def source(query):
        return [c_compilation, c_exact]
    result = asyncio.run(search_sources(SourceRegistry([SourceEntry("a", "1", source)]), "周杰伦 晴天"))
    assert [c.item_id for c in result.candidates] == ["1", "2"]
