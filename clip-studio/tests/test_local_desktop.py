import json
import sqlite3
from pathlib import Path

import pytest

from scripts import local_desktop


TOKEN = "a" * 64


def _external_fixture(tmp_path: Path):
    volumes = tmp_path / "Volumes"
    volume = volumes / "Clip Vault"
    volume.mkdir(parents=True)
    bundle = volumes / "Transcend" / "Private.sparsebundle"
    bundle.parent.mkdir()
    bundle.mkdir()
    data = volume / "Clip Studio" / "data"
    assets = data / "assets" / "asset-1"
    assets.mkdir(parents=True)
    (assets / "original.mp4").write_bytes(b"original")
    (volume / ".clip-studio-storage").write_text(TOKEN, encoding="ascii")
    database = data / "studio.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE assets (body TEXT);"
            "CREATE TABLE projects (id TEXT);"
            "CREATE TABLE jobs (id TEXT);"
        )
        connection.execute(
            "INSERT INTO assets(body) VALUES (?)",
            (json.dumps({"id": "asset-1", "original": "original.mp4"}),),
        )
    config = {
        "CLIP_STORAGE_VOLUME": str(volume),
        "CLIP_STORAGE_IMAGE": str(bundle),
        "CLIP_DATA_DIR": str(data),
        "CLIP_STORAGE_TOKEN": TOKEN,
    }
    return volumes, volume, bundle, data, config


def test_external_storage_validates_mount_identity_and_migrated_database(tmp_path, monkeypatch):
    volumes, volume, bundle, data, config = _external_fixture(tmp_path)
    mounted = {volume, bundle.parent}
    monkeypatch.setattr(local_desktop.os.path, "ismount", lambda path: Path(path) in mounted)

    resolved, verify = local_desktop.resolve_external_storage(config, tmp_path / "support", volumes)

    assert resolved == data
    verify()
    mounted.remove(volume)
    with pytest.raises(RuntimeError, match="not mounted|disconnected"):
        verify()


def test_external_storage_rejects_symlink_escape_and_wrong_volume_token(tmp_path, monkeypatch):
    volumes, volume, bundle, data, config = _external_fixture(tmp_path)
    monkeypatch.setattr(local_desktop.os.path, "ismount", lambda path: Path(path) in {volume, bundle.parent})
    outside = tmp_path / "outside"
    outside.mkdir()
    data.rename(data.with_name("data-real"))
    data.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="inside CLIP_STORAGE_VOLUME"):
        local_desktop.resolve_external_storage(config, tmp_path / "support", volumes)

    volumes2, volume2, bundle2, data2, config2 = _external_fixture(tmp_path / "wrong-token")
    monkeypatch.setattr(local_desktop.os.path, "ismount", lambda path: Path(path) in {volume2, bundle2.parent})
    config2["CLIP_STORAGE_TOKEN"] = "b" * 64
    with pytest.raises(RuntimeError, match="identity changed"):
        local_desktop.resolve_external_storage(config2, tmp_path / "support", volumes2)


def test_load_config_preserves_credentials_and_external_storage_settings(tmp_path):
    config = {
        "CLIP_USERNAME": "owner",
        "CLIP_PASSWORD": "private-password",
        "CLIP_STORAGE_VOLUME": "/Volumes/Clip Vault",
        "CLIP_STORAGE_IMAGE": "/Volumes/Transcend/Private.sparsebundle",
        "CLIP_STORAGE_TOKEN": TOKEN,
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")

    loaded = local_desktop.load_config(tmp_path)

    assert loaded["CLIP_USERNAME"] == "owner"
    assert loaded["CLIP_PASSWORD"] == "private-password"
    assert loaded["CLIP_STORAGE_VOLUME"] == config["CLIP_STORAGE_VOLUME"]
    assert loaded["CLIP_STORAGE_IMAGE"] == config["CLIP_STORAGE_IMAGE"]
    assert loaded["CLIP_STORAGE_TOKEN"] == TOKEN
    assert loaded["CLIP_REQUIRE_AUTH"] == "1"


def test_run_rejects_unavailable_external_volume_before_spawning_services(tmp_path, monkeypatch):
    support = tmp_path / "support"
    data = support / "data"
    data.mkdir(parents=True)
    (data / "keep.txt").write_text("local library stays intact", encoding="utf-8")
    (support / "config.json").write_text(json.dumps({
        "CLIP_STORAGE_VOLUME": "/Volumes/Clip Vault",
        "CLIP_STORAGE_IMAGE": "/Volumes/Transcend/Private.sparsebundle",
        "CLIP_DATA_DIR": "/Volumes/Clip Vault/Clip Studio/data",
        "CLIP_STORAGE_TOKEN": TOKEN,
    }), encoding="utf-8")
    project = tmp_path / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").touch()
    (project / "frontend" / "dist").mkdir(parents=True)
    (project / "frontend" / "dist" / "index.html").touch()
    monkeypatch.setattr(local_desktop, "support_dir", lambda: support)
    monkeypatch.setattr(local_desktop.subprocess, "Popen", lambda *a, **k: pytest.fail("service spawned"))
    monkeypatch.setattr(local_desktop, "resolve_external_storage", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("External data volume is not mounted")))

    with pytest.raises(RuntimeError, match="not mounted"):
        local_desktop.run(project)

    assert (data / "keep.txt").read_text(encoding="utf-8") == "local library stays intact"
