"""Immutable, content-addressed storage for trusted plugin metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from musicdl.contracts import MAX_SOURCE_BYTES, PluginLanguage, PluginManifest
from musicdl.contracts.plugin import Operation


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY = "registry.json"


@dataclass(frozen=True)
class StoredPlugin:
    manifest: PluginManifest
    path: Path
    enabled: bool = True


class PluginStore:
    """Store plugin source below an application-data root.

    A digest path is write-once.  The registry is the only mutable state and
    is replaced atomically after every install or enable-state change.
    """

    def __init__(self, app_data_root: str | os.PathLike[str]):
        self._root = Path(app_data_root).resolve()
        self._plugins = self._root / "plugins"
        self._ensure_directory(self._plugins)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            path.mkdir(mode=0o700, parents=False)
            info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"plugin storage directory is not a real directory: {path}")
        if os.name != "nt":
            path.chmod(0o700)

    def _open_plugin_dir_posix(self, plugin_id: str) -> tuple[int, int]:
        """Open plugins/id anchored to directory fds (POSIX race resistance)."""
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent_fd = os.open(self._plugins, directory_flags)
        try:
            try:
                os.mkdir(plugin_id, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            child_fd = os.open(plugin_id, directory_flags, dir_fd=parent_fd)
            mode = stat.S_IMODE(os.fstat(child_fd).st_mode)
            if mode != 0o700:
                os.fchmod(child_fd, 0o700)
                if stat.S_IMODE(os.fstat(child_fd).st_mode) != 0o700:
                    raise OSError("plugin directory mode must be 0700")
            return parent_fd, child_fd
        except BaseException:
            os.close(parent_fd)
            raise

    @staticmethod
    def _manifest_data(manifest: PluginManifest) -> dict:
        return manifest.model_dump(mode="json")

    def _read_registry(self) -> dict:
        path = self._plugins / _REGISTRY
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise OSError("registry must not be a symlink")
        except FileNotFoundError:
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid plugin registry") from exc
        if not isinstance(value, dict):
            raise ValueError("invalid plugin registry")
        return value

    def _write_registry(self, registry: dict) -> None:
        payload = json.dumps(registry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        temporary = self._plugins / f".{_REGISTRY}.{secrets.token_hex(8)}.tmp"
        fd = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                fd = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._plugins / _REGISTRY)
            if hasattr(os, "O_DIRECTORY"):
                dir_fd = os.open(self._plugins, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _validate_id(plugin_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", plugin_id):
            raise ValueError("invalid plugin id")

    def install(
        self,
        *,
        plugin_id: str,
        version: str,
        language: PluginLanguage,
        operations: Iterable[Operation],
        allowed_hosts: Iterable[str],
        source: str,
    ) -> StoredPlugin:
        self._validate_id(plugin_id)
        source_bytes = source.encode("utf-8")
        if len(source_bytes) > MAX_SOURCE_BYTES or b"\x00" in source_bytes:
            raise ValueError("source exceeds storage limits")
        digest = hashlib.sha256(source_bytes).hexdigest()
        manifest = PluginManifest(
            plugin_id=plugin_id,
            version=version,
            language=language,
            operations=tuple(operations),
            allowed_hosts=tuple(allowed_hosts),
            sha256=digest,
        )
        suffix = ".py" if language == "python" else ".js"
        plugin_dir = self._plugins / plugin_id
        created = False
        source_ready = False
        parent_fd = child_fd = None
        try:
            if os.name != "nt":
                parent_fd, child_fd = self._open_plugin_dir_posix(plugin_id)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                try:
                    fd = os.open(f"{digest}{suffix}", flags, 0o600, dir_fd=child_fd)
                    created = True
                except FileExistsError:
                    fd = None
                if created:
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(source_bytes)
                            stream.flush()
                            os.fsync(stream.fileno())
                        info = os.stat(f"{digest}{suffix}", dir_fd=child_fd, follow_symlinks=False)
                        if stat.S_IMODE(info.st_mode) != 0o600:
                            raise OSError("plugin source mode must be 0600")
                    except BaseException:
                        try:
                            os.unlink(f"{digest}{suffix}", dir_fd=child_fd)
                        except FileNotFoundError:
                            pass
                        raise
                check_fd = os.open(f"{digest}{suffix}", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=child_fd)
                try:
                    info = os.fstat(check_fd)
                    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                        raise OSError("plugin source must be a regular 0600 file")
                    chunks = []
                    while sum(map(len, chunks)) <= len(source_bytes):
                        chunk = os.read(check_fd, len(source_bytes) + 1 - sum(map(len, chunks)))
                        if not chunk:
                            break
                        chunks.append(chunk)
                    if b"".join(chunks) != source_bytes:
                        raise ValueError("plugin version is immutable")
                finally:
                    os.close(check_fd)
                source_ready = True
            else:
                self._ensure_directory(plugin_dir)
                target = plugin_dir / f"{digest}{suffix}"
                try:
                    existing = target.lstat()
                except FileNotFoundError:
                    existing = None
                if existing is None:
                    try:
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        created = True
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(source_bytes)
                            stream.flush()
                    except FileExistsError:
                        existing = target.lstat()
                    except BaseException:
                        if created:
                            target.unlink(missing_ok=True)
                        raise
                if target.is_symlink() or not target.is_file() or target.read_bytes() != source_bytes:
                    raise ValueError("plugin version is immutable")
                source_ready = True
        except BaseException:
            if created and not source_ready:
                try:
                    if child_fd is not None:
                        os.unlink(f"{digest}{suffix}", dir_fd=child_fd)
                    else:
                        target.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if child_fd is not None:
                os.close(child_fd)
            if parent_fd is not None:
                os.close(parent_fd)
        target = plugin_dir / f"{digest}{suffix}"

        registry = self._read_registry()
        versions = registry.setdefault(plugin_id, {})
        entry = versions.get(digest)
        manifest_data = self._manifest_data(manifest)
        if entry is not None and (entry.get("manifest") != manifest_data):
            raise ValueError("plugin version metadata is immutable")
        if entry is None:
            versions[digest] = {"manifest": manifest_data, "enabled": True}
            self._write_registry(registry)
        return StoredPlugin(manifest=manifest, path=target, enabled=(entry or {}).get("enabled", True))

    def load(self, plugin_id: str, sha256: str) -> StoredPlugin:
        self._validate_id(plugin_id)
        if not _DIGEST.fullmatch(sha256):
            raise ValueError("invalid plugin digest")
        registry = self._read_registry()
        entry = registry.get(plugin_id, {}).get(sha256)
        if not entry:
            raise KeyError((plugin_id, sha256))
        manifest = PluginManifest.model_validate(entry["manifest"])
        suffix = ".py" if manifest.language == "python" else ".js"
        path = self._plugins / plugin_id / f"{sha256}{suffix}"
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("plugin source must not be a symlink or non-file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError("plugin source mode must be 0600")
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha256:
            raise ValueError("stored source digest mismatch")
        return StoredPlugin(manifest, path, bool(entry.get("enabled", True)))

    def enabled(self) -> tuple[StoredPlugin, ...]:
        result = []
        for plugin_id, versions in self._read_registry().items():
            for digest, entry in versions.items():
                if entry.get("enabled", True):
                    result.append(self.load(plugin_id, digest))
        return tuple(result)

    def set_enabled(self, plugin_id: str, sha256: str, enabled: bool) -> StoredPlugin:
        registry = self._read_registry()
        try:
            registry[plugin_id][sha256]["enabled"] = bool(enabled)
        except KeyError as exc:
            raise KeyError((plugin_id, sha256)) from exc
        self._write_registry(registry)
        return self.load(plugin_id, sha256)
