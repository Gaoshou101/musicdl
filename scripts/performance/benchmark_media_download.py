"""Synthetic local benchmark for the existing media download path.

Run the same file against different source trees by selecting the implementation
through PYTHONPATH, for example::

    PYTHONPATH=src python scripts/performance/benchmark_media_download.py
    PYTHONPATH=/path/to/baseline/src python /path/to/modified/scripts/performance/benchmark_media_download.py

This measures local chunk delivery, hashing, duration inspection, filesystem
publication, and event-loop responsiveness. It does not model network latency or
claim a production throughput forecast. Its deterministic, parser-readable MP3
frames are synthetic bytes, not an audio fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from musicdl.media.download import download_candidate
from musicdl.media.models import DownloadMetadata
from musicdl.sources.models import Candidate


MIB = 1024 * 1024
FRAME_HEADER = bytes((0xFF, 0xFB, 0x90, 0x00))
FRAME_LENGTH = 144 * 128_000 // 44_100
FRAME_SAMPLES = 1_152
ID3_HEADER = b"ID3\x04\x00\x00\x00\x00\x00\x00"
CHUNK_SIZE = 128 * 1024
MAX_SCRATCH_MIB = 64


class SyntheticLocalSource:
    """A repeatable in-process source that yields one valid parser-shaped stream."""

    def __init__(self, payload: bytes):
        self.payload = payload

    async def download(self, _candidate: Candidate, *, quality: str | None = None) -> DownloadMetadata:
        async def chunks():
            for offset in range(0, len(self.payload), CHUNK_SIZE):
                yield self.payload[offset:offset + CHUNK_SIZE]
                await asyncio.sleep(0)

        return DownloadMetadata(chunks(), extension="mp3", media_type="audio/mpeg",
                                declared_size=len(self.payload), quality=quality)


def synthetic_payload(size_mib: int, seed: int) -> tuple[bytes, int, int]:
    target_bytes = size_mib * MIB
    frame_count = (target_bytes - len(ID3_HEADER) + FRAME_LENGTH - 1) // FRAME_LENGTH
    body = random.Random(seed).randbytes(FRAME_LENGTH - len(FRAME_HEADER))
    frame = FRAME_HEADER + body
    payload = ID3_HEADER + frame * frame_count
    duration = round(frame_count * FRAME_SAMPLES / 44_100)
    return payload, frame_count, duration


def _percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percent
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _rss_bytes() -> tuple[int | None, str | None]:
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB; macOS reports bytes.
        return int(value if sys.platform == "darwin" else value * 1024), "resource.ru_maxrss"
    except (ImportError, AttributeError, OSError):
        try:
            import psutil  # type: ignore[import-not-found]

            return int(psutil.Process().memory_info().rss), "psutil.rss"
        except (ImportError, OSError):
            return None, None


def _concurrency_values(value: str) -> list[int]:
    try:
        values = [int(part.strip()) for part in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("concurrency must be comma-separated positive integers") from None
    if not values or any(number < 1 or number > 16 for number in values):
        raise argparse.ArgumentTypeError("concurrency values must be between 1 and 16")
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("concurrency values must be unique")
    return values


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mib", type=int, default=4, help="synthetic stream size (4 through 16 MiB)")
    parser.add_argument("--rounds", type=int, default=2, help="serial rounds at each concurrency level")
    parser.add_argument("--concurrency", type=_concurrency_values, default=[1, 4, 8, 16],
                        help="comma-separated simultaneous downloads (maximum 16)")
    parser.add_argument("--seed", type=int, default=1_296_671_049, help="fixed payload seed")
    args = parser.parse_args(argv)
    if not 4 <= args.size_mib <= 16:
        parser.error("size-mib must be between 4 and 16")
    if not 1 <= args.rounds <= 20:
        parser.error("rounds must be between 1 and 20")
    if args.size_mib * max(args.concurrency) > MAX_SCRATCH_MIB:
        parser.error(f"size-mib × max concurrency must stay within the {MAX_SCRATCH_MIB} MiB scratch budget")
    return args


async def _lag_monitor(stop: asyncio.Event, samples: list[float], thread_counts: list[int]) -> None:
    interval = 0.01
    expected = asyncio.get_running_loop().time() + interval
    while not stop.is_set():
        await asyncio.sleep(interval)
        now = asyncio.get_running_loop().time()
        samples.append(max(0.0, now - expected))
        expected = now + interval
        thread_counts.append(threading.active_count())


async def _download_one(source: SyntheticLocalSource, payload_size: int, duration: int,
                        root: Path, *, sequence: int, round_number: int) -> tuple[float, Path, int]:
    candidate = Candidate(
        source_id="synthetic-local", source_version="benchmark-v1",
        item_id=f"seed-{sequence}-round-{round_number}", title=f"Bench {sequence} {round_number}",
        artist="Fixed Synthetic Artist", duration=duration, format="mp3", size=payload_size,
    )
    started = time.perf_counter()
    result = await download_candidate(candidate, source, root, request_id=f"bench-{sequence}-{round_number}")
    elapsed = time.perf_counter() - started
    return elapsed, root / result.relative_path, result.size_bytes


async def _run_level(source: SyntheticLocalSource, payload_size: int, duration: int,
                     root: Path, concurrency: int, rounds: int) -> dict[str, Any]:
    latencies: list[float] = []
    lag_samples: list[float] = []
    thread_counts = [threading.active_count()]
    stop = asyncio.Event()
    monitor = asyncio.create_task(_lag_monitor(stop, lag_samples, thread_counts))
    started = time.perf_counter()
    total_bytes = 0
    try:
        for round_number in range(rounds):
            tasks = [asyncio.create_task(_download_one(
                source, payload_size, duration, root, sequence=number, round_number=round_number,
            )) for number in range(concurrency)]
            results = await asyncio.gather(*tasks)
            latencies.extend(item[0] for item in results)
            total_bytes += sum(item[2] for item in results)
            for _, path, _ in results:
                path.unlink(missing_ok=True)
    finally:
        stop.set()
        await monitor
    elapsed = time.perf_counter() - started
    rss_bytes, rss_source = _rss_bytes()
    return {
        "concurrency": concurrency,
        "rounds": rounds,
        "downloads": len(latencies),
        "bytes": total_bytes,
        "elapsed_seconds": elapsed,
        "throughput_mib_s": total_bytes / MIB / elapsed if elapsed else None,
        "latency_seconds_p50": _percentile(latencies, 0.50),
        "latency_seconds_p95": _percentile(latencies, 0.95),
        "event_loop_lag_seconds_p50": _percentile(lag_samples, 0.50),
        "event_loop_lag_seconds_p95": _percentile(lag_samples, 0.95),
        "event_loop_lag_seconds_max": max(lag_samples, default=0.0),
        "peak_rss_bytes": rss_bytes,
        "peak_rss_source": rss_source,
        "peak_threads": max(thread_counts, default=threading.active_count()),
    }


async def _main(args: argparse.Namespace) -> None:
    payload, frame_count, duration = synthetic_payload(args.size_mib, args.seed)
    source = SyntheticLocalSource(payload)
    implementation = Path(__import__("musicdl.media.download", fromlist=["__file__"]).__file__).resolve()
    metadata = {
        "type": "metadata",
        "benchmark": "musicdl-local-media-download-v1",
        "synthetic": True,
        "network": False,
        "payload_seed": args.seed,
        "requested_size_mib": args.size_mib,
        "payload_bytes": len(payload),
        "mp3_frames": frame_count,
        "candidate_duration_seconds": duration,
        "chunk_size_bytes": CHUNK_SIZE,
        "rounds": args.rounds,
        "concurrency": args.concurrency,
        "scratch_budget_mib": MAX_SCRATCH_MIB,
        "implementation_file": str(implementation),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    print(json.dumps(metadata, sort_keys=True))
    with tempfile.TemporaryDirectory(prefix="musicdl-media-bench-") as directory:
        root = Path(directory)
        for concurrency in args.concurrency:
            result = await _run_level(source, len(payload), duration, root, concurrency, args.rounds)
            print(json.dumps({"type": "result", **result}, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
