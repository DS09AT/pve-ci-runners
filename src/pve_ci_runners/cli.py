"""The `pve-ci-runners` command."""

import argparse
import logging
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from pve_ci_runners import config as configuration
from pve_ci_runners._version import VERSION
from pve_ci_runners.actions import Actions
from pve_ci_runners.cache import CacheBackend, NoCache
from pve_ci_runners.cache_zfs import ZfsCache
from pve_ci_runners.controller import Controller
from pve_ci_runners.doctor import Doctor
from pve_ci_runners.drain import drain, resume
from pve_ci_runners.errors import PveCiError
from pve_ci_runners.github import AppKeySigner, GitHubClient
from pve_ci_runners.overflow import Overflow
from pve_ci_runners.proc import HostCommands
from pve_ci_runners.pve import Pve
from pve_ci_runners.status import collect
from pve_ci_runners.template_build import TemplateBuilder

_ATTACH_INTERVAL = 2.0
log = logging.getLogger("pve_ci_runners")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit status."""
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except PveCiError as err:
        print(f"pve-ci-runners: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pve-ci-runners", description="GitHub Actions jobs in fresh Proxmox VE VMs."
    )
    parser.add_argument(
        "-c", "--config", type=Path, default=configuration.DEFAULT_PATH, help="configuration file"
    )
    commands = parser.add_subparsers(required=True, metavar="command")
    commands.add_parser("run", help="run the controller in the foreground").set_defaults(handler=_run)
    check = commands.add_parser("config", help="configuration commands")
    check_commands = check.add_subparsers(required=True, metavar="action")
    check_commands.add_parser("check", help="validate the configuration and exit").set_defaults(
        handler=_check
    )
    status = commands.add_parser("status", help="show what every slot does")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(handler=_status)
    commands.add_parser("doctor", help="check the node and the GitHub App").set_defaults(handler=_doctor)
    template = commands.add_parser("template", help="runner template commands")
    template_commands = template.add_subparsers(required=True, metavar="action")
    template_commands.add_parser("build", help="build the next runner template").set_defaults(
        handler=_build_template
    )
    commands.add_parser("drain", help="take no new jobs; running jobs finish").set_defaults(handler=_drain)
    commands.add_parser("resume", help="take new jobs again").set_defaults(handler=_resume)
    commands.add_parser("version", help="print the version").set_defaults(handler=_version)
    return parser


def _check(args: argparse.Namespace) -> int:
    loaded = configuration.load(args.config)
    print(f"{args.config}: valid, {len(loaded.slots)} slots, {len(loaded.profiles)} profiles")
    return 0


def _status(args: argparse.Namespace) -> int:
    loaded = configuration.load(args.config)
    result = collect(loaded, Pve(HostCommands()))
    print(result.to_json() if args.json else result.to_text())
    return 0


def _drain(args: argparse.Namespace) -> int:
    drain(configuration.load(args.config).daemon.state_dir)
    print("draining: no new jobs are taken, running jobs finish")
    return 0


def _resume(args: argparse.Namespace) -> int:
    resume(configuration.load(args.config).daemon.state_dir)
    print("resumed: new jobs are taken")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    loaded = configuration.load(args.config)
    commands = HostCommands()
    findings = Doctor(loaded, commands, Pve(commands), _client(loaded, commands)).run()
    for finding in findings:
        print(finding.to_text())
    return 1 if any(f.level == "fail" for f in findings) else 0


def _build_template(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    vmid, version = TemplateBuilder(configuration.load(args.config), Pve(HostCommands())).build()
    print(f"template {vmid} built with runner {version}")
    return 0


def _version(_: argparse.Namespace) -> int:
    print(VERSION)
    return 0


def _run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    loaded = configuration.load(args.config)
    configuration.check_secret_file(loaded.github.app_key_file)
    commands = HostCommands()
    actions = Actions(_client(loaded, commands), allowed=loaded.github.repositories)
    cache: CacheBackend = ZfsCache(loaded.cache, commands) if loaded.cache.backend == "zfs" else NoCache()
    overflow = Overflow(loaded.overflow, actions) if loaded.overflow else None
    controller = Controller(loaded, Pve(commands), actions, cache, overflow=overflow)
    log.info(
        "controller %s up: %d slots, profiles %s",
        VERSION,
        len(loaded.slots),
        [p.name for p in loaded.profiles],
    )
    while True:
        _cycle(controller)
        deadline = time.monotonic() + loaded.daemon.poll_seconds
        while time.monotonic() < deadline:
            _attach(controller)
            time.sleep(_ATTACH_INTERVAL)


def _client(loaded: configuration.Config, commands: HostCommands) -> GitHubClient:
    return GitHubClient(
        api_url=loaded.github.api_url,
        client_id=loaded.github.app_client_id,
        owner=loaded.github.owner,
        signer=AppKeySigner(commands, loaded.github.app_key_file),
    )


def _cycle(controller: Controller) -> None:
    try:
        controller.cycle()
    except PveCiError as err:
        log.warning("cycle incomplete: %s", err)
    except Exception:
        # One failed cycle must not stop the controller; the next one starts from the host's state.
        log.exception("cycle failed")


def _attach(controller: Controller) -> None:
    try:
        controller.attach_pending()
    except PveCiError as err:
        log.warning("attach incomplete: %s", err)
    except Exception:
        # Like a failed cycle, a failed attach round must not stop the controller.
        log.exception("attach failed")
