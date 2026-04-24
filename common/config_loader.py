"""Unified akopia.yaml loader with env interpolation + JSON Schema validation.

Reads `akopia.yaml` (path from env `AKOPIA_CONFIG_PATH`, default
`./akopia.yaml`), interpolates `${VAR}` / `${VAR:-default}` references
from the OS environment, validates against `common/akopia_schema.json`,
and returns a typed `AkopiaConfig`.

On failure, raises `ConfigError` with a YAML path pointer (e.g.
`sources[2].config.token: missing env AKOPIA_GITHUB_TOKEN`).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from jsonschema import Draft7Validator
from pydantic import ValidationError

from common.akopia_config import AkopiaConfig

__all__ = ["ConfigLoader", "ConfigError", "load_config"]


class ConfigError(Exception):
    """Raised when akopia.yaml cannot be loaded, interpolated, or validated.

    The message always carries a YAML-path pointer where possible
    (e.g. `core.storage.vector.url: invalid URL`).
    """


# ${VAR} or ${VAR:-default}
# Default may contain any char except closing brace.
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_DEFAULT_CONFIG_PATH_ENV = "AKOPIA_CONFIG_PATH"
_DEFAULT_CONFIG_PATH = "./akopia.yaml"
_SCHEMA_FILE = Path(__file__).parent / "akopia_schema.json"


def _format_path(path: list[Any]) -> str:
    """Render a list like ['sources', 2, 'config', 'token'] as
    `sources[2].config.token`."""
    parts: list[str] = []
    for seg in path:
        if isinstance(seg, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{seg}]"
            else:
                parts.append(f"[{seg}]")
        else:
            parts.append(str(seg))
    return ".".join(parts) if parts else "<root>"


def _interpolate_env(value: Any, path: list[Any], env: dict[str, str]) -> Any:
    """Walk the loaded YAML tree, substituting `${VAR}` / `${VAR:-default}`
    tokens in string values. Raises ConfigError pointing at the YAML path
    when a required var is missing."""
    if isinstance(value, str):
        return _interpolate_string(value, path, env)
    if isinstance(value, list):
        return [_interpolate_env(v, path + [i], env) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _interpolate_env(v, path + [k], env) for k, v in value.items()}
    return value


def _interpolate_string(value: str, path: list[Any], env: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)  # None if no `:-default` given
        if var_name in env:
            return env[var_name]
        if default is not None:
            return default
        raise ConfigError(
            f"{_format_path(path)}: missing env {var_name}"
        )

    return _ENV_PATTERN.sub(_replace, value)


class ConfigLoader:
    """Loads, interpolates, and validates akopia.yaml.

    Usage:
        loader = ConfigLoader()           # picks up AKOPIA_CONFIG_PATH / ./akopia.yaml
        cfg = loader.load()               # returns AkopiaConfig

        loader = ConfigLoader(path="foo") # explicit path
        cfg = loader.load()
    """

    def __init__(
        self,
        path: Optional[str | os.PathLike[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._env = env if env is not None else dict(os.environ)
        self._schema = self._load_schema()

    @staticmethod
    def _load_schema() -> dict[str, Any]:
        with _SCHEMA_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _resolve_path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        env_path = self._env.get(_DEFAULT_CONFIG_PATH_ENV)
        return Path(env_path) if env_path else Path(_DEFAULT_CONFIG_PATH)

    def load(self) -> AkopiaConfig:
        cfg_path = self._resolve_path()
        if not cfg_path.exists():
            raise ConfigError(f"akopia.yaml not found at: {cfg_path}")

        try:
            with cfg_path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{cfg_path}: YAML parse error: {exc}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(
                f"{cfg_path}: top-level YAML must be a mapping, got {type(raw).__name__}"
            )

        interpolated = _interpolate_env(raw, [], self._env)
        self._validate_schema(interpolated)
        return self._to_model(interpolated)

    def _validate_schema(self, data: dict[str, Any]) -> None:
        validator = Draft7Validator(self._schema)
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        if errors:
            # Surface the first few errors (keep message tight but informative).
            rendered = []
            for err in errors[:5]:
                loc = _format_path(list(err.absolute_path))
                rendered.append(f"{loc}: {err.message}")
            raise ConfigError("config validation failed:\n  - " + "\n  - ".join(rendered))

    @staticmethod
    def _to_model(data: dict[str, Any]) -> AkopiaConfig:
        try:
            return AkopiaConfig.model_validate(data)
        except ValidationError as exc:
            # Pydantic should rarely fail after JSON-schema passes, but
            # surface a YAML-path-shaped message if it does.
            lines: list[str] = []
            for err in exc.errors():
                loc = _format_path(list(err["loc"]))
                lines.append(f"{loc}: {err['msg']}")
            raise ConfigError("config validation failed:\n  - " + "\n  - ".join(lines)) from exc


def load_config(path: Optional[str | os.PathLike[str]] = None) -> AkopiaConfig:
    """Convenience wrapper: one-shot load with default env."""
    return ConfigLoader(path=path).load()
