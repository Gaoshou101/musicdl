import hashlib
import json
import os
import stat

import pytest

from musicdl.plugins import store as store_module
from musicdl.plugins.store import PluginStore


SOURCE = "# demo\nprint('ok')\n"


def install(store, *, plugin_id="demo", version="1", language="python", source=SOURCE):
    return store.install(
        plugin_id=plugin_id,
        version=version,
        language=language,
        operations=("search",),
        allowed_hosts=(),
        source=source,
    )


def test_install_hashes_source_and_loads_immutable_version(tmp_path):
    store = PluginStore(tmp_path)
    stored = install(store)

    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    assert stored.manifest.sha256 == digest
    assert stored.path == tmp_path / "plugins" / "demo" / f"{digest}.py"
    assert stored.path.read_bytes() == SOURCE.encode()
    if os.name != "nt":
        assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600
    loaded = store.load("demo", digest)
    assert loaded.manifest == stored.manifest
    assert loaded.path.read_bytes() == SOURCE.encode()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_install_repairs_source_mode_when_open_ignores_requested_mode(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    real_open = store_module.os.open
    real_fchmod = store_module.os.fchmod

    def open_with_insecure_mode(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & store_module.os.O_CREAT and mode == 0o600:
            real_fchmod(fd, 0o700)
        return fd

    monkeypatch.setattr(store_module.os, "open", open_with_insecure_mode)
    stored = install(store)

    assert stat.S_IMODE(stored.path.stat().st_mode) == 0o600
    loaded = store.load("demo", stored.manifest.sha256)
    assert loaded.manifest == stored.manifest


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_install_rejects_unrepairable_source_mode_and_cleans_created_file(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    real_open = store_module.os.open
    real_fchmod = store_module.os.fchmod

    def open_with_insecure_mode(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & store_module.os.O_CREAT and mode == 0o600:
            real_fchmod(fd, 0o700)
        return fd

    def ineffective_fchmod(fd, mode):
        if mode != 0o600:
            real_fchmod(fd, mode)

    monkeypatch.setattr(store_module.os, "open", open_with_insecure_mode)
    monkeypatch.setattr(store_module.os, "fchmod", ineffective_fchmod)
    with pytest.raises(OSError) as exc_info:
        install(store)

    assert exc_info.value.args == ("plugin source mode must be 0600",)
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    assert not (tmp_path / "plugins" / "demo" / f"{digest}.py").exists()


def test_install_same_version_is_idempotent_but_mismatch_is_rejected(tmp_path):
    store = PluginStore(tmp_path)
    first = install(store)
    second = install(store)
    assert second.manifest == first.manifest
    assert second.path == first.path
    first.path.write_bytes(b"different")
    with pytest.raises(ValueError, match="immutable"):
        install(store)


def test_javascript_uses_js_extension(tmp_path):
    stored = install(PluginStore(tmp_path), language="javascript")
    assert stored.path.suffix == ".js"


def test_rejects_symlinked_plugin_directory_and_file(tmp_path):
    store = PluginStore(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "plugins" / "demo").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        install(store)

    clean = tmp_path / "clean"
    clean.mkdir()
    file_store = PluginStore(clean)
    first = install(file_store)
    first.path.unlink()
    first.path.symlink_to(outside / "source.py")
    with pytest.raises((OSError, ValueError)):
        install(file_store)


def test_rejects_traversal_plugin_id(tmp_path):
    with pytest.raises(ValueError):
        install(PluginStore(tmp_path), plugin_id="../escape")


def test_registry_replace_failure_cleans_temporary_file(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    original_replace = os.replace

    def fail_replace(src, dst):
        if str(dst).endswith("registry.json"):
            raise OSError("injected replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        install(store)
    registry_dir = tmp_path / "plugins"
    assert not list(registry_dir.glob(".registry.json.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX fsync cleanup assertion")
def test_source_write_failure_cleans_new_file(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    original_fsync = os.fsync

    def fail_source_fsync(fd):
        raise OSError("injected source fsync failure")

    monkeypatch.setattr(os, "fsync", fail_source_fsync)
    with pytest.raises(OSError, match="source fsync"):
        install(store)
    digest = hashlib.sha256(SOURCE.encode()).hexdigest()
    assert not (tmp_path / "plugins" / "demo" / f"{digest}.py").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission assertion")
def test_load_rejects_source_with_insecure_mode(tmp_path):
    store = PluginStore(tmp_path)
    stored = install(store)
    stored.path.chmod(0o644)
    with pytest.raises(OSError, match="0600"):
        store.load("demo", stored.manifest.sha256)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-relative load assertion")
def test_posix_load_does_not_read_source_by_path(tmp_path, monkeypatch):
    store = PluginStore(tmp_path)
    stored = install(store)
    original = type(stored.path).read_bytes

    def fail_path_read(self):
        if self == stored.path:
            raise AssertionError("source must be read through an anchored fd")
        return original(self)

    monkeypatch.setattr(type(stored.path), "read_bytes", fail_path_read)
    assert store.load("demo", stored.manifest.sha256).manifest == stored.manifest


def test_enabled_state_is_independent_per_version(tmp_path):
    store = PluginStore(tmp_path)
    one = install(store, version="1")
    two = install(store, version="2", source="print('two')")
    store.set_enabled("demo", one.manifest.sha256, False)
    enabled = store.enabled()
    assert [item.manifest.version for item in enabled] == ["2"]
    assert store.load("demo", one.manifest.sha256).enabled is False
    assert store.load("demo", two.manifest.sha256).enabled is True
    registry = json.loads((tmp_path / "plugins" / "registry.json").read_text())
    assert registry["demo"][one.manifest.sha256]["enabled"] is False
    assert registry["demo"][two.manifest.sha256]["enabled"] is True


def test_remove_drops_the_registry_entry_and_the_stored_bytes(tmp_path):
    store = PluginStore(tmp_path)
    stored = install(store)

    assert store.remove("demo") == (stored.manifest.sha256,)
    assert store.enabled() == ()
    assert not stored.path.exists()
    with pytest.raises(KeyError):
        store.load("demo", stored.manifest.sha256)
    with pytest.raises(KeyError):
        store.remove("demo")


def test_remove_one_version_leaves_the_other_installed(tmp_path):
    store = PluginStore(tmp_path)
    one = install(store, version="1")
    two = install(store, version="2", source="print('two')\n")

    assert store.remove("demo", one.manifest.sha256) == (one.manifest.sha256,)
    assert not one.path.exists()
    assert two.path.exists()
    assert [item.manifest.version for item in store.enabled()] == ["2"]
    with pytest.raises(KeyError):
        store.remove("demo", one.manifest.sha256)


def test_remove_rejects_an_invalid_digest_or_id(tmp_path):
    store = PluginStore(tmp_path)
    install(store)
    with pytest.raises(ValueError, match="digest"):
        store.remove("demo", "not-a-digest")
    with pytest.raises(ValueError, match="invalid plugin id"):
        store.remove("../escape")
