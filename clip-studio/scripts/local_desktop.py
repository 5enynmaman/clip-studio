#!/usr/bin/env python3
"""Run Clip Studio's local web and worker services for the native macOS app."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


APP_NAME = "Clip Studio"
HOST = "127.0.0.1"
PORT = 8765
_STOP = False


def support_dir() -> Path:
    override = os.environ.get("CLIP_STUDIO_SUPPORT_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def load_config(folder: Path) -> dict[str, str]:
    path = folder / "config.json"
    values: dict[str, str] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise RuntimeError(f"{path} must contain a JSON object")
        values = {str(key): str(value) for key, value in loaded.items() if value is not None}
    values.setdefault("CLIP_USERNAME", "clip-local")
    values.setdefault("CLIP_PASSWORD", secrets.token_urlsafe(36))
    values["CLIP_REQUIRE_AUTH"] = "1"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.pending")
    temporary.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return values


def _mounted_parent(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for parent in (candidate, *candidate.parents):
        if os.path.ismount(parent):
            return parent.resolve()
    return None


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return path != root
    except ValueError:
        return False


def _validate_migrated_data(data_dir: Path) -> None:
    database = data_dir / "studio.sqlite3"
    if not database.is_file() or database.stat().st_size == 0:
        raise RuntimeError("External Clip Studio data is not migrated (studio.sqlite3 is missing)")
    if not (data_dir / "assets").is_dir():
        raise RuntimeError("External Clip Studio data is incomplete (assets folder is missing)")
    try:
        uri = f"file:{database.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not result or result[0] != "ok" or not {"assets", "projects", "jobs"}.issubset(tables):
                raise RuntimeError("External Clip Studio database is not a complete migrated library")
            assets = [json.loads(row[0]) for row in connection.execute("SELECT body FROM assets")]
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise RuntimeError("External Clip Studio database could not be validated") from exc
    for asset in assets:
        asset_id, original = asset.get("id"), asset.get("original")
        if not asset_id or not original or not (data_dir / "assets" / asset_id / original).is_file():
            raise RuntimeError("External Clip Studio library has a missing original asset")


def resolve_external_storage(config: dict[str, str], folder: Path,
                             volumes_root: Path = Path("/Volumes")) -> tuple[Path, object]:
    """Validate mount identity and migrated data before any service can write."""
    folder = folder.resolve()
    volume_value = config.get("CLIP_STORAGE_VOLUME")
    image_value = config.get("CLIP_STORAGE_IMAGE")
    data_value = config.get("CLIP_DATA_DIR")
    external_requested = bool(volume_value or image_value)
    if not external_requested:
        data_dir = Path(data_value).expanduser().resolve() if data_value else folder / "data"
        if data_value and not _is_within(data_dir, folder):
            raise RuntimeError("CLIP_DATA_DIR outside Application Support requires external storage configuration")
        return data_dir, lambda: None
    if not volume_value or not image_value or not data_value:
        raise RuntimeError("External storage requires CLIP_STORAGE_VOLUME, CLIP_STORAGE_IMAGE and CLIP_DATA_DIR")
    volume_raw, image_raw, data_raw = map(lambda value: Path(value).expanduser(),
                                          (volume_value, image_value, data_value))
    if not all(path.is_absolute() for path in (volume_raw, image_raw, data_raw)):
        raise RuntimeError("External storage paths must be absolute")
    try:
        volume = volume_raw.resolve(strict=True)
        image = image_raw.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("The configured external volume or sparsebundle is unavailable") from exc
    volumes_root = volumes_root.resolve(strict=False)
    if not _is_within(volume, volumes_root) or not os.path.ismount(volume) or not volume.is_dir():
        raise RuntimeError(f"External data volume is not mounted: {volume}")
    if not (image.is_file() or image.is_dir()):
        raise RuntimeError("The configured sparsebundle does not exist")
    image_volume = _mounted_parent(image)
    if image_volume is None or not _is_within(image_volume, volumes_root):
        raise RuntimeError("The sparsebundle's external storage volume is not mounted")
    data_dir = data_raw.resolve(strict=False)
    if not _is_within(data_dir, volume):
        raise RuntimeError("CLIP_DATA_DIR must resolve inside CLIP_STORAGE_VOLUME; symlink escapes are rejected")
    token = config.get("CLIP_STORAGE_TOKEN", "")
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise RuntimeError("External storage identity is missing or invalid in config.json")
    marker = volume / ".clip-studio-storage"

    def verify_mount() -> None:
        if not os.path.ismount(volume):
            raise RuntimeError(f"External storage disconnected; stopping Clip Studio: {volume}")
        if _mounted_parent(image) != image_volume:
            raise RuntimeError("Sparsebundle storage volume disconnected; stopping Clip Studio")
        if not _is_within(data_dir.resolve(strict=False), volume):
            raise RuntimeError("External data path changed or escaped its mounted volume; stopping Clip Studio")
        try:
            if marker.is_symlink() or marker.read_text(encoding="ascii").strip() != token:
                raise RuntimeError("External storage identity changed; stopping Clip Studio")
        except OSError as exc:
            raise RuntimeError("External storage identity is unavailable; stopping Clip Studio") from exc

    verify_mount()
    _validate_migrated_data(data_dir)
    return data_dir, verify_mount


def _signal(signum: int, frame: object) -> None:
    del signum, frame
    global _STOP
    _STOP = True


def _port_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, PORT))
        except OSError:
            return False
        return True


def _stop_process(process: subprocess.Popen[bytes] | None, label: str, timeout: float = 20) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    # Each service owns a separate session; kill only that service's descendants.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print(f"Could not stop {label} cleanly", file=sys.stderr, flush=True)


def _wait_ready(base_url: str, username: str, password: str, web: subprocess.Popen[bytes],
                worker: subprocess.Popen[bytes], storage_check: object = None) -> bool:
    import base64
    import urllib.error
    import urllib.request

    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = urllib.request.Request(base_url + "/api/health", headers={"Authorization": f"Basic {token}"})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not _STOP:
        if storage_check:
            storage_check()
        if web.poll() is not None or worker.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200 and json.loads(response.read()).get("ok"):
                    return True
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(.25)
    return False


def run(project_root: Path) -> int:
    global _STOP
    _STOP = False
    project_root = project_root.expanduser().resolve()
    python = project_root / ".venv" / "bin" / "python"
    if not python.is_file() or not (project_root / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Clip Studio is not built. Run scripts/build-desktop.sh from the project folder.")

    folder = support_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    config = load_config(folder)
    data_dir, storage_check = resolve_external_storage(config, folder)
    external_storage = bool(config.get("CLIP_STORAGE_VOLUME") or config.get("CLIP_STORAGE_IMAGE"))
    if not external_storage:
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(data_dir, 0o700)
    else:
        storage_check()

    homebrew = Path("/opt/homebrew/bin")
    env = os.environ.copy()
    env.update(config)
    env.update({
        "CLIP_DATA_DIR": str(data_dir),
        "CLIP_RUNTIME": "desktop",
        "PYTHONPATH": str(project_root),
        "PYTHONUNBUFFERED": "1",
        "PATH": os.pathsep.join([str(python.parent), str(homebrew), "/usr/bin", "/bin", env.get("PATH", "")]),
    })
    ffmpeg_full = Path("/opt/homebrew/opt/ffmpeg-full/bin")
    if (ffmpeg_full / "ffmpeg").is_file():
        env["CLIP_FFMPEG"] = str(ffmpeg_full / "ffmpeg")
        env["CLIP_FFPROBE"] = str(ffmpeg_full / "ffprobe")
    elif shutil.which("ffmpeg", path=env["PATH"]):
        env.pop("CLIP_FFMPEG", None)
        env.pop("CLIP_FFPROBE", None)
    else:
        raise RuntimeError("FFmpeg was not found. Install Homebrew ffmpeg-full and try again.")

    logs = folder / "logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(logs, 0o700)

    lock_path = folder / "runtime.lock"
    lock = lock_path.open("a+")
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("Clip Studio is already running. Switch to the open app window.") from exc

    if not _port_available():
        raise RuntimeError(f"Port {PORT} is already in use. Close the app using that port and retry.")

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)
    logs = folder / "logs"
    worker_log = (logs / "worker.log").open("ab", buffering=0)
    web_log = (logs / "web.log").open("ab", buffering=0)
    os.chmod(logs / "worker.log", 0o600)
    os.chmod(logs / "web.log", 0o600)
    worker: subprocess.Popen[bytes] | None = None
    web: subprocess.Popen[bytes] | None = None
    try:
        worker = subprocess.Popen([str(python), "-m", "backend.worker"], cwd=project_root, env=env,
                                  stdin=subprocess.DEVNULL, stdout=worker_log, stderr=worker_log,
                                  start_new_session=True)
        web = subprocess.Popen([str(python), "-m", "uvicorn", "backend.app:app", "--host", HOST,
                                "--port", str(PORT), "--workers", "1", "--no-access-log"],
                               cwd=project_root, env=env, stdin=subprocess.DEVNULL,
                               stdout=web_log, stderr=web_log, start_new_session=True)
        base_url = f"http://{HOST}:{PORT}"
        if not _wait_ready(base_url, env["CLIP_USERNAME"], env["CLIP_PASSWORD"], web, worker,
                           storage_check=storage_check if external_storage else None):
            raise RuntimeError("Clip Studio did not become ready. Check logs in Application Support/Clip Studio/logs.")
        print("Clip Studio local services are ready.", flush=True)
        while not _STOP:
            if external_storage:
                storage_check()
            if worker.poll() is not None or web.poll() is not None:
                raise RuntimeError("A local Clip Studio service stopped. Check logs in Application Support/Clip Studio/logs.")
            time.sleep(.25)
        return 0
    finally:
        _stop_process(web, "web service")
        _stop_process(worker, "worker service")
        web_log.close()
        worker_log.close()
        lock.close()


def check(project_root: Path) -> None:
    project_root = project_root.expanduser().resolve()
    python = project_root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise RuntimeError(f"Missing executable: {python}")
    if not (project_root / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Missing frontend/dist/index.html; build the frontend first.")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root)
    subprocess.run([str(python), "-c", "import fastapi, uvicorn, backend.app, backend.worker"], cwd=project_root, env=env, check=True)
    ffmpeg = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
    if not ffmpeg.is_file():
        ffmpeg = Path(shutil.which("ffmpeg") or "")
    if not ffmpeg.is_file():
        raise RuntimeError("FFmpeg is not installed")
    filters = subprocess.run([str(ffmpeg), "-hide_banner", "-filters"], capture_output=True, text=True, check=True).stdout
    missing = [name for name in ("drawtext", "subtitles", "zscale") if name not in filters]
    if missing:
        raise RuntimeError("FFmpeg is missing required filters: " + ", ".join(missing))


def initialize_config() -> None:
    folder = support_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    load_config(folder)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="validate local runtime without starting services")
    parser.add_argument("--init-config", action="store_true", help="create private local auth settings without starting services")
    args = parser.parse_args()
    try:
        if args.init_config:
            initialize_config()
            print("Local Clip Studio settings are ready.")
            return 0
        if args.check:
            check(args.project_root)
            print("Local Clip Studio runtime is ready.")
            return 0
        return run(args.project_root)
    except Exception as exc:
        print(f"Clip Studio: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
