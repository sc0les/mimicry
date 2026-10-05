#!/usr/bin/env python3
"""Create a consistent plaintext backup archive ready for age encryption."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import ibkr_state


ROOT = Path(__file__).resolve().parent
BACKUP_FILES = (
    "browser_state.json",
    "figi_cache.json",
    "ibkr_contract_cache.json",
    "last_state.json",
    "last_state_ibkr.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def create_backup(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = output_dir / f"mimicry-state-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="mimicry-backup-") as temp_name:
        staging = Path(temp_name)
        if ibkr_state.STATE_DB_FILE.exists():
            ibkr_state.backup_database(
                ibkr_state.STATE_DB_FILE,
                staging / ibkr_state.STATE_DB_FILE.name,
            )
        for name in BACKUP_FILES:
            source = ROOT / name
            if source.exists():
                shutil.copy2(source, staging / name)
        copied = sorted(path for path in staging.iterdir() if path.is_file())
        metadata = {
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hostname": platform.node(),
            "python": platform.python_version(),
            "git_commit": git_commit(),
            "files": {
                path.name: {
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
                for path in copied
            },
            "required_secret_names": [
                "IBKR_ACCOUNT_ID",
                "IBKR_GATEWAY_HOST",
                "IBKR_GATEWAY_PORT",
                "PUSHOVER_APP_TOKEN",
                "PUSHOVER_USER_KEY",
                "BACKUP_AGE_RECIPIENT",
                "BACKUP_RCLONE_REMOTE",
            ],
        }
        (staging / "backup_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True)
        )
        with tarfile.open(archive, "w:gz") as tar:
            for path in sorted(staging.iterdir()):
                tar.add(path, arcname=path.name)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Mimicry state backup archive")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(create_backup(args.output_dir))


if __name__ == "__main__":
    main()
