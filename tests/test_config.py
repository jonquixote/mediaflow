"""Config loader, path derivation and reload fingerprint tests.

[SAF-10] no secrets in config.toml
[SAF-11] dry_run defaults to true
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaflow.config import (  # noqa: E402
    DEFAULT_PRESETS_DIR,
    DEFAULT_RUNTIME_DIR,
    DEFAULT_STATE_DIR,
    Config,
    ConfigError,
    config_mtime_fingerprint,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _quote(value: object) -> str:
    return json.dumps(str(value))


def write_config(
    tmp_path: Path,
    body: str = "",
    *,
    root: Path | None = None,
    filename: str = "config.toml",
) -> Path:
    """Write a minimal valid config rooted inside tmp_path."""

    root = root if root is not None else tmp_path / "media"
    path = tmp_path / filename
    text = f"root = {_quote(root)}\n"
    if body:
        text += body
        if not text.endswith("\n"):
            text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_match_spec_section_10(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path), {})

    assert config.root == tmp_path / "media"
    assert config.ffmpeg == "/usr/lib/jellyfin-ffmpeg/ffmpeg"
    assert config.ffprobe == "/usr/lib/jellyfin-ffmpeg/ffprobe"
    # SAF-11: a fresh config is dry-run.
    assert config.dry_run is True
    assert config.poll_seconds == 30
    assert config.stable_seconds == 120
    assert config.trash_retention_days == 14
    assert config.failed_retention_days == 30
    assert config.min_free_gb == 40
    assert config.root_min_free_gb == 2
    assert config.max_trash_gb == 120
    assert config.space_watchdog_seconds == 60
    assert config.append_tmdbid is False
    assert config.pause_when_jellyfin_transcoding is True
    assert config.pause_on_battery is True
    assert config.pause_method == "sigstop"
    assert config.temp_sensor == "auto"
    assert config.start_if_temp_below_c == 65
    assert config.thermal_pause_c == 85
    assert config.thermal_resume_c == 75
    assert config.progress_seconds == 30
    assert config.stall_minutes == 10
    assert config.max_restarts == 3
    assert config.min_output_kbps == 300
    assert config.min_bpp == 0.01
    assert config.review_reminder_days == 3
    assert config.notify_cooldown_hours == 6
    assert config.jellyfin_user == "jellyfin"
    assert config.state_dir == DEFAULT_STATE_DIR
    assert config.runtime_dir == DEFAULT_RUNTIME_DIR


def test_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml", {})


def test_invalid_toml_is_reported_without_echoing_contents(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('root = "/tmp/x"\nsecret_thing = "supersecret"\n[broken\n')
    with pytest.raises(ConfigError, match="invalid TOML") as excinfo:
        load_config(path, {})
    assert "supersecret" not in str(excinfo.value)


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, "poll_second = 10\n")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(path, {})


def test_unknown_table_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, "\n[presets]\nenabled = true\n")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_config(path, {})


@pytest.mark.parametrize(
    "key,value",
    [
        ("tmdb_read_token", "TOKEN-VALUE-DO-NOT-PRINT"),
        ("tmdb_token", "TOKEN-VALUE-DO-NOT-PRINT"),
        ("notify_url", "https://ntfy.example/secret-topic"),
        ("webhook_url", "https://example.invalid/hook"),
        ("api_key", "KEY-VALUE"),
    ],
)
def test_secret_keys_in_config_name_saf_10(
    tmp_path: Path, key: str, value: str
) -> None:
    path = write_config(tmp_path, f'{key} = "{value}"\n')
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, {})
    message = str(excinfo.value)
    assert "SAF-10" in message
    assert value not in message
    assert key in message


def test_nested_secret_key_in_config_is_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '\n[secrets]\nnotify_url = "https://example.invalid/very-secret"\n'
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, {})
    message = str(excinfo.value)
    assert "SAF-10" in message
    assert "very-secret" not in message


def test_notify_table_url_is_rejected_as_a_secret(tmp_path: Path) -> None:
    path = write_config(
        tmp_path, '\n[notify]\nurl = "https://example.invalid/notify-secret"\n'
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, {})
    message = str(excinfo.value)
    assert "SAF-10" in message
    assert "notify-secret" not in message


def test_mediaflow_root_overrides_config_root(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    path = write_config(tmp_path, root=tmp_path / "from-file")

    config = load_config(path, {"MEDIAFLOW_ROOT": str(sandbox)})

    assert config.root == sandbox
    assert config.state_dir == sandbox / "_state"
    assert config.runtime_dir == sandbox / "_run"
    assert config.db_path == sandbox / "_state" / "state.db"
    assert config.lock_path == sandbox / "_run" / "daemon.lock"


def test_explicit_state_and_runtime_are_not_replaced_by_env(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    state = tmp_path / "explicit-state"
    runtime = tmp_path / "explicit-run"
    body = f"state_dir = {_quote(state)}\nruntime_dir = {_quote(runtime)}\n"
    path = write_config(tmp_path, body)

    config = load_config(path, {"MEDIAFLOW_ROOT": str(sandbox)})

    assert config.root == sandbox
    assert config.state_dir == state
    assert config.runtime_dir == runtime


@pytest.mark.parametrize(
    "production",
    [
        "/srv/media",
        "/srv/media/",
        "/srv/media/../media",
        "/srv/media/.",
        "/srv/media/movies",
        "/srv/media/_pipeline/work",
    ],
)
def test_mediaflow_root_refuses_production(
    tmp_path: Path, production: str
) -> None:
    path = write_config(tmp_path)
    with pytest.raises(ConfigError, match="/srv/media") as excinfo:
        load_config(path, {"MEDIAFLOW_ROOT": production})
    assert "production" in str(excinfo.value)


def test_relative_mediaflow_root_is_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    with pytest.raises(ConfigError, match="absolute"):
        load_config(path, {"MEDIAFLOW_ROOT": "relative/sandbox"})


def test_relative_config_paths_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('root = "relative/media"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="absolute"):
        load_config(path, {})


@pytest.mark.parametrize(
    "body,needle",
    [
        ('dry_run = "yes"\n', "dry_run"),
        ("poll_seconds = true\n", "poll_seconds"),
        ("trash_retention_days = -1\n", "trash_retention_days"),
        ("failed_retention_days = -1\n", "failed_retention_days"),
        ("min_free_gb = -0.5\n", "min_free_gb"),
        ("max_trash_gb = -1\n", "max_trash_gb"),
        ("space_watchdog_seconds = 0\n", "space_watchdog_seconds"),
        ("progress_seconds = 0\n", "progress_seconds"),
        ("stall_minutes = 0\n", "stall_minutes"),
        ("max_restarts = -1\n", "max_restarts"),
        ("min_output_kbps = -1\n", "min_output_kbps"),
        ("min_bpp = -0.1\n", "min_bpp"),
        ("review_reminder_days = -1\n", "review_reminder_days"),
        ("notify_cooldown_hours = -1\n", "notify_cooldown_hours"),
        # SAF-2: the full decode is mandatory and cannot be weakened by config.
        ('verify_level = "quick"\n', "verify_level"),
        ('pause_method = "kill"\n', "pause_method"),
        ('temp_sensor = "coretemp"\n', "temp_sensor"),
        ('jellyfin_user = ""\n', "jellyfin_user"),
        ('ffmpeg = ""\n', "ffmpeg"),
        ('ffprobe = ""\n', "ffprobe"),
        ("state_dir = 5\n", "state_dir"),
        ("runtime_dir = 5\n", "runtime_dir"),
        ("thermal_pause_c = 70.0\nthermal_resume_c = 80.0\n", "thermal_resume_c"),
    ],
)
def test_type_and_range_validation(tmp_path: Path, body: str, needle: str) -> None:
    path = write_config(tmp_path, body)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, {})
    assert needle in str(excinfo.value)


def test_ensure_directories_is_setgid_and_never_experimental(
    tmp_path: Path,
) -> None:
    sandbox = tmp_path / "sandbox"
    path = write_config(tmp_path)
    config = load_config(path, {"MEDIAFLOW_ROOT": str(sandbox)})

    touched = config.ensure_directories()

    # A root created by ensure_directories is setgid too; a pre-existing root
    # (a mount point in production) is left with its own ownership policy.
    assert stat.S_IMODE(config.root.stat().st_mode) == 0o2775

    for directory in (
        config.movies,
        config.pipeline,
        config.inbox,
        config.convert_root,
        config.convert,
        config.work,
        config.review,
        config.failed,
        config.trash,
        config.state_dir,
        config.backup_dir,
        config.log_dir,
        config.runtime_dir,
    ):
        assert directory.is_dir(), directory
        mode = stat.S_IMODE(directory.stat().st_mode)
        assert mode == 0o2775, (directory, oct(mode))
        assert mode & stat.S_ISGID
        assert directory in touched

    # install.sh must never create the experimental folder.
    assert not (sandbox / "presets").exists()
    assert not (sandbox / "presets" / "experimental").exists()
    assert not (sandbox / "_pipeline" / "experimental").exists()


def test_paths_and_path_map_cover_doctor_targets(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    path = write_config(tmp_path)
    config = load_config(path, {"MEDIAFLOW_ROOT": str(sandbox)})

    paths = config.paths()
    assert isinstance(paths, list)
    assert all(isinstance(item, Path) for item in paths)
    for expected in (
        config.movies,
        config.inbox,
        config.convert,
        config.work,
        config.review,
        config.failed,
        config.trash,
        config.db_path,
        config.backup_dir,
        config.log_dir,
        config.capabilities_path,
        config.status_path,
        config.progress_path,
        config.lock_path,
    ):
        assert expected in paths

    mapping = config.path_map()
    assert mapping["movies"] == config.movies
    assert mapping["convert"] == config.convert
    assert mapping["db"] == config.db_path
    assert len(mapping) == len(set(mapping.values()))


def test_direct_config_normalises_paths(tmp_path: Path) -> None:
    config = Config(root=str(tmp_path))
    assert isinstance(config.root, Path)
    assert config.movies == tmp_path / "movies"
    assert config.convert == tmp_path / "_pipeline" / "convert" / "movies"


def test_fingerprint_changes_on_config_and_preset_changes(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    presets = tmp_path / "presets.d"
    presets.mkdir()
    preset = presets / "one.toml"
    preset.write_text('id = "one"\n', encoding="utf-8")

    first = config_mtime_fingerprint(path, presets)
    assert first == config_mtime_fingerprint(path, presets)
    assert len(first) == 64
    int(first, 16)

    # Deterministic mtime bump; no sleeps.
    current = path.stat()
    os.utime(path, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
    second = config_mtime_fingerprint(path, presets)
    assert second != first

    third_path = presets / "two.toml"
    third_path.write_text('id = "two"\n', encoding="utf-8")
    third = config_mtime_fingerprint(path, presets)
    assert third != second

    # Missing paths are represented, not raised.
    missing = config_mtime_fingerprint(tmp_path / "absent.toml", tmp_path / "absent")
    assert len(missing) == 64


def test_missing_presets_dir_is_stable_for_fingerprint(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    assert config_mtime_fingerprint(path, DEFAULT_PRESETS_DIR) == (
        config_mtime_fingerprint(path, DEFAULT_PRESETS_DIR)
    )


def test_example_config_is_complete_and_loadable(tmp_path: Path) -> None:
    example = REPO_ROOT / "config" / "config.toml.example"
    sandbox = tmp_path / "sandbox"
    config = load_config(example, {"MEDIAFLOW_ROOT": str(sandbox)})

    # The template carries the production path overrides and is dry-run first.
    assert config.root == sandbox
    assert config.dry_run is True
    assert config.state_dir == Path("/var/lib/mediaflow")
    assert config.runtime_dir == Path("/run/mediaflow")
    assert config.ffmpeg.endswith("/jellyfin-ffmpeg/ffmpeg")
    assert config.trash_retention_days == 14
    assert config.max_restarts == 3
