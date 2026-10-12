"""Report gzip sizes of static JS/CSS assets and Docker image metadata.

This script reads an already-exported asset directory and already-built Docker
images. It never builds, starts, or modifies an image. Docker's ``Size`` field
is the local uncompressed image size in bytes, not a registry transfer size.

Example::

    python scripts/performance/measure_artifacts.py \
      --assets-dir /tmp/musicdl-panel \
      --image musicdl:ci --image musicdl-plugin-runner:ci \
      --source-commit 0123456789abcdef --snapshot current
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import platform
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any


MIB = 1024 * 1024
ASSET_SUFFIXES = {".css", ".js"}
GZIP_LEVEL = 9


def _asset_records(root: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not root.is_dir():
        raise ValueError(f"asset directory does not exist: {root}")
    records: list[dict[str, Any]] = []
    raw_total = 0
    gzip_total = 0
    files = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in ASSET_SUFFIXES)
    if not files:
        raise ValueError(f"no .js or .css assets found under: {root}")
    for path in files:
        data = path.read_bytes()
        compressed = gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)
        raw_size = len(data)
        gzip_size = len(compressed)
        raw_total += raw_size
        gzip_total += gzip_size
        records.append({
            "path": path.relative_to(root).as_posix(),
            "extension": path.suffix.lower(),
            "raw_bytes": raw_size,
            "gzip_bytes": gzip_size,
        })
    return records, {
        "asset_file_count": len(records),
        "asset_raw_bytes": raw_total,
        "asset_gzip_bytes": gzip_total,
    }


def _docker_image_record(image_name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "image", "inspect", image_name],
        check=True,
        capture_output=True,
        text=True,
    )
    inspected = json.loads(completed.stdout)
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise ValueError(f"docker inspect returned an unexpected result for {image_name}")
    image = inspected[0]
    size = image.get("Size")
    if not isinstance(size, int):
        raise ValueError(f"docker inspect did not return an integer Size for {image_name}")
    config = image.get("Config") or {}
    return {
        "image": image_name,
        "image_id": image.get("Id"),
        "repo_tags": image.get("RepoTags") or [],
        "repo_digests": image.get("RepoDigests") or [],
        "os": image.get("Os") or config.get("Os"),
        "architecture": image.get("Architecture") or config.get("Architecture"),
        "docker_image_size_bytes": size,
        "docker_image_size_mib": size / MIB,
    }


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=Path,
                        help="already-extracted production panel assets, including nested .js/.css files")
    parser.add_argument("--image", action="append", default=[],
                        help="already-built Docker image reference; repeat for both app images")
    parser.add_argument("--source-commit", help="source commit or immutable revision label")
    parser.add_argument("--snapshot", help="baseline/current label for an exported source snapshot")
    args = parser.parse_args(argv)
    if args.assets_dir is None and not args.image:
        parser.error("provide --assets-dir, at least one --image, or both")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    print(json.dumps({
        "type": "metadata",
        "measurement": "musicdl-artifact-size-v1",
        "source_commit": args.source_commit,
        "snapshot": args.snapshot,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "zlib_version": zlib.ZLIB_VERSION,
        "gzip_level": GZIP_LEVEL,
        "asset_extensions": sorted(ASSET_SUFFIXES),
    }, sort_keys=True))

    try:
        if args.assets_dir is not None:
            root = args.assets_dir.resolve()
            records, summary = _asset_records(root)
            print(json.dumps({"type": "asset_summary", "root": str(root), **summary}, sort_keys=True))
            for record in records:
                print(json.dumps({"type": "asset_file", **record}, sort_keys=True))
        for image_name in args.image:
            print(json.dumps({"type": "docker_image", **_docker_image_record(image_name)}, sort_keys=True))
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"artifact measurement failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
