"""Secrets loading, redaction and permission warnings.

[SAF-10] the token/URL come only from env files, are sent as headers, and are
redacted everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mediaflow.config import (  # noqa: E402
    ConfigError,
    Secrets,
    load_secrets,
    redact,
)

TOKEN = "tmdb-token-value-that-must-never-appear"
URL = "https://ntfy.example.invalid/a-very-secret-topic"


def write_env_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "etc-mediaflow"
    directory.mkdir()
    tmdb = directory / "tmdb.env"
    notify = directory / "notify.env"
    tmdb.write_text(f"TMDB_READ_TOKEN={TOKEN}\n", encoding="utf-8")
    notify.write_text(f"NOTIFY_URL={URL}\n", encoding="utf-8")
    os.chmod(tmdb, 0o640)
    os.chmod(notify, 0o640)
    return directory


def test_loads_token_and_url_from_env_files(tmp_path: Path) -> None:
    directory = write_env_dir(tmp_path)
    secrets = load_secrets(directory)

    assert secrets.token == TOKEN
    assert secrets.url == URL
    assert secrets.tmdb_token == TOKEN
    assert secrets.notify_url == URL
    assert secrets.has_token is True
    assert secrets.has_notify_url is True
    assert secrets.warnings == ()


def test_missing_secrets_are_optional(tmp_path: Path) -> None:
    directory = tmp_path / "missing"
    secrets = load_secrets(directory)
    assert secrets.token is None
    assert secrets.url is None
    assert secrets.has_token is False
    assert secrets.warnings == ()

    directory.mkdir()
    secrets = load_secrets(directory)
    assert secrets.token is None
    assert secrets.url is None


def test_token_only_and_url_only(tmp_path: Path) -> None:
    directory = tmp_path / "env"
    directory.mkdir()
    (directory / "tmdb.env").write_text(f"TMDB_READ_TOKEN={TOKEN}\n")
    os.chmod(directory / "tmdb.env", 0o640)
    secrets = load_secrets(directory)
    assert secrets.token == TOKEN
    assert secrets.url is None

    (directory / "tmdb.env").unlink()
    (directory / "notify.env").write_text(f"NOTIFY_URL={URL}\n")
    os.chmod(directory / "notify.env", 0o640)
    secrets = load_secrets(directory)
    assert secrets.token is None
    assert secrets.url == URL


def test_repr_and_str_never_leak_secrets() -> None:
    secrets = Secrets(token=TOKEN, url=URL)
    rendered = repr(secrets) + "\n" + str(secrets)
    assert TOKEN not in rendered
    assert URL not in rendered
    assert "***" in rendered


def test_redact_helper() -> None:
    assert redact(None) == ""
    assert redact("") == ""
    redacted = redact(TOKEN)
    assert redacted == "***"
    assert TOKEN not in redacted


def test_permission_warning_is_non_fatal_and_redacted(tmp_path: Path) -> None:
    directory = write_env_dir(tmp_path)
    os.chmod(directory / "tmdb.env", 0o644)

    warning_list: list[str] = []
    secrets = load_secrets(directory, warnings=warning_list)

    assert secrets.token == TOKEN
    assert warning_list
    assert secrets.warnings == tuple(warning_list)
    joined = "\n".join(warning_list)
    assert "0640" in joined
    assert TOKEN not in joined
    assert URL not in joined


def test_0600_is_acceptable(tmp_path: Path) -> None:
    directory = write_env_dir(tmp_path)
    os.chmod(directory / "tmdb.env", 0o600)
    os.chmod(directory / "notify.env", 0o600)
    secrets = load_secrets(directory)
    assert secrets.warnings == ()


@pytest.mark.parametrize("mode", [0o604, 0o640 + 0o002, 0o640 + 0o010])
def test_more_open_modes_warn(tmp_path: Path, mode: int) -> None:
    directory = write_env_dir(tmp_path)
    os.chmod(directory / "notify.env", mode)
    secrets = load_secrets(directory)
    assert any("notify.env" in warning for warning in secrets.warnings)
    assert URL not in "\n".join(secrets.warnings)


def test_comments_export_quotes_and_unknown_keys(tmp_path: Path) -> None:
    directory = tmp_path / "env"
    directory.mkdir()
    (directory / "tmdb.env").write_text(
        "\n".join(
            [
                "# a comment",
                "IGNORED_KEY=value",
                f'export TMDB_READ_TOKEN="{TOKEN}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (directory / "notify.env").write_text(
        f"NOTIFY_URL='{URL}#fragment'  # trailing comment\n",
        encoding="utf-8",
    )
    os.chmod(directory / "tmdb.env", 0o640)
    os.chmod(directory / "notify.env", 0o640)

    secrets = load_secrets(directory)
    assert secrets.token == TOKEN
    # A URL fragment is part of the URL, not a comment.
    assert secrets.url == URL + "#fragment"


def test_malformed_env_file_is_reported_without_value(tmp_path: Path) -> None:
    directory = tmp_path / "env"
    directory.mkdir()
    path = directory / "tmdb.env"
    path.write_text("THIS_IS_NOT_ASSIGNMENT\n", encoding="utf-8")
    os.chmod(path, 0o640)

    with pytest.raises(ConfigError, match="invalid line") as excinfo:
        load_secrets(directory)
    assert "THIS_IS_NOT_ASSIGNMENT" not in str(excinfo.value)


def test_invalid_key_is_reported_without_value(tmp_path: Path) -> None:
    directory = tmp_path / "env"
    directory.mkdir()
    path = directory / "tmdb.env"
    path.write_text(f"BAD KEY={TOKEN}\n", encoding="utf-8")
    os.chmod(path, 0o640)

    with pytest.raises(ConfigError, match="invalid key") as excinfo:
        load_secrets(directory)
    assert TOKEN not in str(excinfo.value)


def test_env_file_not_world_readable_for_service(tmp_path: Path) -> None:
    directory = write_env_dir(tmp_path)
    for path in (directory / "tmdb.env", directory / "notify.env"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o007 == 0
