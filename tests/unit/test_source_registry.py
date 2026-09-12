import pytest

from musicdl.sources.registry import SourceEntry, SourceRegistry


class Source:
    async def search(self, query):
        return []


def test_registry_rejects_duplicate_ids_and_filters_enabled():
    registry = SourceRegistry()
    registry.register(SourceEntry("a", "1", Source(), enabled=True, priority=2))
    registry.register(SourceEntry("b", "1", Source(), enabled=False, priority=1))
    with pytest.raises(ValueError):
        registry.register(SourceEntry("a", "2", Source()))
    assert [entry.source_id for entry in registry.enabled()] == ["a"]


def test_registry_rejects_invalid_identity_and_adapter():
    with pytest.raises(ValueError): SourceRegistry([SourceEntry(" ", "1", Source())])
    with pytest.raises(ValueError): SourceRegistry([SourceEntry("a", " ", Source())])
    with pytest.raises(ValueError): SourceRegistry([SourceEntry("a", "1", object())])
    with pytest.raises(ValueError, match="invalid_source_id"): SourceRegistry([SourceEntry(1, "1", Source())])

def test_registry_rejects_non_boolean_enabled_and_caps_sources():
    with pytest.raises(ValueError, match="invalid_enabled"):
        SourceRegistry([SourceEntry("a", "1", Source(), enabled="false")])
    with pytest.raises(ValueError, match="too_many_sources"):
        SourceRegistry([SourceEntry(str(i), "1", Source()) for i in range(3)], max_sources=2)
