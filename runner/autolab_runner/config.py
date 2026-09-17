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
        if not self.test_mode:
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
        if self.gpu_slots < 1:
            errors.append("gpu_slots must be >= 1")
        if errors:
            raise ConfigError("runner config invalid: " + "; ".join(errors))

    @classmethod
    def load(cls, config_path: str | Path) -> RunnerConfig:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        if not cls._is_test_candidate(path):
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ConfigError(
                    f"config file {path} must not be group/world readable (chmod 600); mode={oct(mode)}"
                )
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw.setdefault("test_mode", False)
        if raw["test_mode"] is True:
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

    @staticmethod
    def _is_test_candidate(path: Path) -> bool:
        # a config explicitly in test mode is chmod'ed below; non-test configs
        # must already be private before we even parse them.
        try:
            with open(path, encoding="utf-8") as fh:
                return bool(json.load(fh).get("test_mode"))
        except Exception:
            return False


def load_config_or_die(config_path: str | Path) -> RunnerConfig:
    try:
        return RunnerConfig.load(config_path)
    except ConfigError as exc:
        print(f"autolab-runner: {exc}", file=sys.stderr)
        raise
