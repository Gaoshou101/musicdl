"""Measure controlled source-search orchestration and download admission waits.

This benchmark calls the production ``search_sources`` orchestration with
bounded in-memory sources and measures ``DownloadAdmission`` in separate
controlled queue scenarios. It does not contact music providers or measure an
HTTP request. Admission wait is reported independently from search and media
download timing; the held permit only creates a repeatable queue condition.

Example::

    PYTHONPATH=src python scripts/performance/benchmark_search_admission.py \
      --source-commit 0123456789abcdef --snapshot current
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import time
from pathlib import Path
from typing import Any

import musicdl.media.admission as media_admission
import musicdl.sources.search as source_search
from musicdl.media.admission import DownloadAdmission
from musicdl.sources.models import Candidate
from musicdl.sources.registry import SourceEntry, SourceRegistry


def _positive_int(value: str, *, name: str, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{name} must be a positive integer") from None
    if not 1 <= parsed <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be between 1 and {maximum}")
    return parsed


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


class SyntheticSearchSource:
    """Return a fixed candidate catalogue through the normal async source API."""

    def __init__(self, candidates: tuple[Candidate, ...]):
        self._candidates = candidates

    async def search(self, _query: str) -> tuple[Candidate, ...]:
        # Yield so search_sources exercises its concurrent fan-out scheduling.
        # This is a scheduler yield, not a simulated network delay.
        await asyncio.sleep(0)
        return self._candidates


def _search_registry(source_count: int, results_per_source: int) -> SourceRegistry:
    entries: list[SourceEntry] = []
    for source_number in range(source_count):
        source_id = f"bench-source-{source_number:02d}"
        candidates = tuple(Candidate(
            source_id=source_id,
            source_version="benchmark-v1",
            item_id=f"track-{item_number:04d}",
            title=f"Synthetic Track {item_number:04d}",
            artist=f"Synthetic Artist {item_number % 11:02d}",
            album=f"Synthetic Album {item_number % 7:02d}",
            duration=180 + item_number % 90,
            bitrate=320,
            format="mp3",
            size=7_200_000 + item_number,
        ) for item_number in range(results_per_source))
        entries.append(SourceEntry(
            source_id=source_id,
            version="benchmark-v1",
            source=SyntheticSearchSource(candidates),
            priority=source_number,
        ))
    return SourceRegistry(entries, max_sources=max(1, source_count))


async def _measure_search(registry: SourceRegistry, rounds: int,
                          results_per_source: int) -> dict[str, Any]:
    latencies: list[float] = []
    unique_candidates = 0
    statuses: tuple[str, ...] = ()
    started = time.perf_counter()
    for _ in range(rounds):
        request_started = time.perf_counter()
        result = await source_search.search_sources(
            registry, "synthetic benchmark query", timeout=5.0,
            max_results_per_source=results_per_source,
        )
        latencies.append(time.perf_counter() - request_started)
        unique_candidates = len(result.candidates)
        statuses = tuple(status.status for status in result.statuses)
    elapsed = time.perf_counter() - started
    return {
        "type": "result",
        "phase": "search_orchestration",
        "rounds": rounds,
        "source_count": len(statuses),
        "results_per_source": results_per_source,
        "unique_candidates_per_search": unique_candidates,
        "source_statuses_last_search": list(statuses),
        "elapsed_seconds": elapsed,
        "searches_per_second": rounds / elapsed if elapsed else None,
        "search_latency_seconds_p50": _percentile(latencies, 0.50),
        "search_latency_seconds_p95": _percentile(latencies, 0.95),
    }


async def _measure_admission_wait(*, scope: str, active_limit: int,
                                  waiters: int, hold_ms: int) -> dict[str, Any]:
    source_limit = min(2, active_limit)
    admission = DownloadAdmission(
        active_limit=active_limit,
        pending_limit=max(16, waiters),
        per_source_limit=source_limit,
        per_user_limit=active_limit,
    )
    source_id = "benchmark-source" if scope == "per_source" else None
    held_contexts = []
    tasks: list[asyncio.Task[float]] = []

    async def wait_for_permit() -> float:
        queued_at = time.perf_counter()
        async with admission.acquire(source_id=source_id, timeout=10.0):
            return time.perf_counter() - queued_at

    try:
        held_count = source_limit if scope == "per_source" else active_limit
        for _ in range(held_count):
            context = admission.acquire(source_id=source_id)
            await context.__aenter__()
            held_contexts.append(context)

        tasks = [asyncio.create_task(wait_for_permit()) for _ in range(waiters)]
        loop = asyncio.get_running_loop()
        queue_deadline = loop.time() + 5.0
        while admission.snapshot()["pending"] < waiters:
            if loop.time() >= queue_deadline:
                raise TimeoutError("benchmark waiters did not reach the admission queue")
            await asyncio.sleep(0)

        await asyncio.sleep(hold_ms / 1000)
        await asyncio.gather(*(
            context.__aexit__(None, None, None) for context in reversed(held_contexts)
        ))
        held_contexts.clear()
        wait_times = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if held_contexts:
            await asyncio.gather(*(
                context.__aexit__(None, None, None) for context in reversed(held_contexts)
            ), return_exceptions=True)

    return {
        "type": "result",
        "phase": "admission_wait",
        "scope": scope,
        "active_limit": active_limit,
        "per_source_limit": source_limit,
        "waiters": waiters,
        "controlled_hold_seconds": hold_ms / 1000,
        "queue_wait_seconds_p50": _percentile(wait_times, 0.50),
        "queue_wait_seconds_p95": _percentile(wait_times, 0.95),
        "queue_wait_seconds_max": max(wait_times, default=0.0),
    }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-commit", help="source commit or immutable revision label")
    parser.add_argument("--snapshot", help="baseline/current label for an exported source snapshot")
    parser.add_argument("--search-sources", type=lambda value: _positive_int(value, name="search-sources", maximum=16),
                        default=4, help="controlled sources in the search fan-out (maximum 16)")
    parser.add_argument("--search-results", type=lambda value: _positive_int(value, name="search-results", maximum=128),
                        default=32, help="synthetic candidates returned by each source")
    parser.add_argument("--search-rounds", type=lambda value: _positive_int(value, name="search-rounds", maximum=1000),
                        default=20, help="serial calls to the search orchestration")
    parser.add_argument("--active-limit", type=lambda value: _positive_int(value, name="active-limit", maximum=16),
                        default=4, help="active permits held while admission waiters queue")
    parser.add_argument("--waiters", type=lambda value: _positive_int(value, name="waiters", maximum=16),
                        default=8, help="waiters measured in each admission scenario (maximum 16)")
    parser.add_argument("--hold-ms", type=int, default=25,
                        help="controlled permit hold that creates the queue (0 through 5000 ms)")
    args = parser.parse_args(argv)
    if not 0 <= args.hold_ms <= 5000:
        parser.error("hold-ms must be between 0 and 5000")
    return args


async def _main(args: argparse.Namespace) -> None:
    print(json.dumps({
        "type": "metadata",
        "benchmark": "musicdl-search-admission-v1",
        "synthetic": True,
        "network": False,
        "source_commit": args.source_commit,
        "snapshot": args.snapshot,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "search_implementation_file": str(Path(source_search.__file__).resolve()),
        "admission_implementation_file": str(Path(media_admission.__file__).resolve()),
        "search_sources": args.search_sources,
        "search_results_per_source": args.search_results,
        "search_rounds": args.search_rounds,
        "admission_active_limit": args.active_limit,
        "admission_waiters": args.waiters,
        "admission_hold_ms": args.hold_ms,
    }, sort_keys=True))

    registry = _search_registry(args.search_sources, args.search_results)
    print(json.dumps(await _measure_search(registry, args.search_rounds, args.search_results), sort_keys=True))
    for scope in ("global", "per_source"):
        result = await _measure_admission_wait(
            scope=scope,
            active_limit=args.active_limit,
            waiters=args.waiters,
            hold_ms=args.hold_ms,
        )
        print(json.dumps(result, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
