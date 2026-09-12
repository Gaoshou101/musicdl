"""Source adapters and deterministic search aggregation."""

from .models import Candidate
from .registry import MusicSource, SourceEntry, SourceRegistry
from .search import SearchResult, SourceStatus, search_sources

__all__ = ["Candidate", "MusicSource", "SourceEntry", "SourceRegistry", "SearchResult", "SourceStatus", "search_sources"]
