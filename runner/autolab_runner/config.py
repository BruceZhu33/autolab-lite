"""Runner configuration, loaded from a 0600 JSON file.

Fail-closed: outside explicit test mode the runner refuses to start without a
token and a manifest HMAC key, and refuses a config file readable by group or
world. Tokens are never accepted on the command line (they would leak into
process listings).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(SystemExit):
    pass


@dataclass
class RunnerConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8420
    token: str = ""
    manifest_hmac_key: str = ""
    allowed_work_roots: list[str] = field(default_factory=list)
    allowed_output_roots: list[str] = field(default_factory=list)
    data_dir: str = ""
    gpu_slots: int = 1
    test_mode: bool = False

    def __post_init__(self) -> None:
        errors: list[str] = []
        if not isinstance(self.test_mode, bool):
            errors.append(f"test_mode must be a boolean, got {self.test_mode!r}")
        if self.test_mode is not True:
            if not self.token:
                errors.append("token is required (or set test_mode for local dev only)")
            if not self.manifest_hmac_key:
                errors.append("manifest_hmac_key is required (or set test_mode)")
        if not self.allowed_work_roots:
            errors.append("allowed_work_roots must list at least one absolute path")
        if not self.allowed_output_roots:
            errors.append("allowed_output_roots must list at least one absolute path")
        if not self.data_dir:
            errors.append("data_dir is required")
        for root in [*self.allowed_work_roots, *self.allowed_output_roots, self.data_dir]:
            if not root.startswith("/"):
                errors.append(f"paths must be absolute: {root!r}")
        if isinstance(self.gpu_slots, bool) or not isinstance(self.gpu_slots, int) or self.gpu_slots < 1:
            errors.append("gpu_slots must be >= 1")
        if errors:
            raise ConfigError("runner config invalid: " + "; ".join(errors))

    @classmethod
    def load(cls, config_path: str | Path) -> RunnerConfig:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        st_mode = stat.S_IMODE(path.stat().st_mode)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid runner config JSON in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"runner config invalid: expected JSON object in {path}")
        if "test_mode" in raw and not isinstance(raw["test_mode"], bool):
            raise ConfigError(
                f"runner config invalid: test_mode must be a boolean, got {raw['test_mode']!r}"
            )
        test_mode = raw.get("test_mode") is True
        if not test_mode and (st_mode & 0o077) != 0:
            raise ConfigError(
                f"config file {path} must not be group/world readable (chmod 600); mode={oct(st_mode)}"
            )
        raw.setdefault("test_mode", False)
        if test_mode:
            # test config may hold generated throwaway creds; still validate structure
            os.chmod(path, 0o600)
        return cls(**{
            key: raw[key]
            for key in (
                "listen_host", "listen_port", "token", "manifest_hmac_key",
                "allowed_work_roots", "allowed_output_roots", "data_dir",
                "gpu_slots", "test_mode",
            )
            if key in raw
        })


def load_config_or_die(config_path: str | Path) -> RunnerConfig:
    try:
        return RunnerConfig.load(config_path)
    except ConfigError as exc:
        print(f"autolab-runner: {exc}", file=sys.stderr)
        raise
