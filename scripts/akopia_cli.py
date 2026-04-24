"""`akopia` CLI entry point.

Subcommands:
    akopia validate [PATH]              Validate akopia.yaml. Exit 0 on success, 1 on failure.
    akopia run adapter <ID> [PATH]      Run the source adapter instance <ID> from akopia.yaml.
    akopia run extractor <TYPE> [PATH]  Run the content extractor plugin <TYPE> from akopia.yaml.

PATH defaults to $AKOPIA_CONFIG_PATH or ./akopia.yaml (same as the loader).

`run adapter` and `run extractor` are the process entry points used by the
docker-compose / k8s deployment manifests — one container per plugin instance.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional, Sequence

from common.config_loader import ConfigError, ConfigLoader
from common.registry import PluginNotFound, default_registry


def _cmd_validate(path: Optional[str]) -> int:
    try:
        cfg = ConfigLoader(path=path).load()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    n_sources = len(cfg.sources)
    n_extractors = len(cfg.extractors)
    print(
        f"ok: akopia.yaml valid (version={cfg.version}, "
        f"sources={n_sources}, extractors={n_extractors})"
    )
    return 0


def _load_config_or_exit(path: Optional[str]):
    try:
        return ConfigLoader(path=path).load()
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def _cmd_run_adapter(instance_id: str, path: Optional[str]) -> int:
    """Instantiate and run one source adapter instance."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config_or_exit(path)
    match = next((s for s in cfg.sources if s.id == instance_id), None)
    if match is None:
        ids = [s.id for s in cfg.sources]
        print(
            f"error: source id {instance_id!r} not found in akopia.yaml. "
            f"Available: {ids}",
            file=sys.stderr,
        )
        return 1
    try:
        cls = default_registry.get_source_adapter_class(match.type)
    except PluginNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    adapter = cls(instance_id=match.id)
    try:
        asyncio.run(adapter.start(match.config))
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_run_extractor(type_name: str, path: Optional[str]) -> int:
    """Instantiate and run one content extractor plugin."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = _load_config_or_exit(path)
    match = next((e for e in cfg.extractors if e.type == type_name), None)
    if match is None:
        types = [e.type for e in cfg.extractors]
        print(
            f"error: extractor type {type_name!r} not found in akopia.yaml. "
            f"Available: {types}",
            file=sys.stderr,
        )
        return 1
    try:
        cls = default_registry.get_extractor_class(match.type)
    except PluginNotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    extractor = cls()
    try:
        asyncio.run(extractor.start(match.config))
    except KeyboardInterrupt:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="akopia",
        description="Akopia CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="Validate akopia.yaml against the schema.",
    )
    validate.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to akopia.yaml (defaults to $AKOPIA_CONFIG_PATH or ./akopia.yaml).",
    )

    run = subparsers.add_parser(
        "run",
        help="Run a plugin instance as a long-lived process.",
    )
    run_sub = run.add_subparsers(dest="target", required=True)

    run_adapter = run_sub.add_parser(
        "adapter",
        help="Run a source adapter instance by its akopia.yaml id.",
    )
    run_adapter.add_argument("id", help="Instance id from akopia.yaml sources[].id")
    run_adapter.add_argument("path", nargs="?", default=None)

    run_extractor = run_sub.add_parser(
        "extractor",
        help="Run a content extractor by its akopia.yaml type.",
    )
    run_extractor.add_argument("type", help="Plugin type from akopia.yaml extractors[].type")
    run_extractor.add_argument("path", nargs="?", default=None)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args.path)
    if args.command == "run":
        if args.target == "adapter":
            return _cmd_run_adapter(args.id, args.path)
        if args.target == "extractor":
            return _cmd_run_extractor(args.type, args.path)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
