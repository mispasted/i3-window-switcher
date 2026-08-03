#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from i3ipc import Event
from i3ipc.aio import Connection


LOG = logging.getLogger("i3-window-switcher")


@dataclass(frozen=True)
class MatchRule:
    window_class: re.Pattern[str] | None = None
    instance: re.Pattern[str] | None = None
    title: re.Pattern[str] | None = None


@dataclass(frozen=True)
class Application:
    name: str
    command: tuple[str, ...]
    match: MatchRule


@dataclass(frozen=True)
class Config:
    settle_delay: float
    applications: dict[str, Application]


def default_config_path() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    return config_home / "i3-window-switcher" / "config.toml"


def runtime_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")

    if not runtime_dir:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")

    return Path(runtime_dir) / "i3-window-switcher.sock"


def compile_optional_regex(
    value: Any,
    *,
    field: str,
    app_name: str,
) -> re.Pattern[str] | None:
    if value is None:
        return None

    if not isinstance(value, str):
        raise ValueError(
            f"applications.{app_name}.match.{field} must be a string"
        )

    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as error:
        raise ValueError(
            f"Invalid regex for applications.{app_name}.match.{field}: "
            f"{error}"
        ) from error


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
    except FileNotFoundError as error:
        raise RuntimeError(f"Configuration file not found: {path}") from error

    general = raw.get("general", {})
    settle_delay = general.get("settle_delay", 1.5)

    if not isinstance(settle_delay, int | float) or settle_delay < 0:
        raise ValueError("general.settle_delay must be a non-negative number")

    raw_apps = raw.get("applications")

    if not isinstance(raw_apps, dict) or not raw_apps:
        raise ValueError(
            "The configuration must contain at least one "
            "[applications.<name>] table"
        )

    applications: dict[str, Application] = {}

    for app_name, raw_app in raw_apps.items():
        if not isinstance(raw_app, dict):
            raise ValueError(f"applications.{app_name} must be a table")

        command = raw_app.get("command")

        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(argument, str) for argument in command)
        ):
            raise ValueError(
                f"applications.{app_name}.command must be a non-empty "
                f"array of strings"
            )

        raw_match = raw_app.get("match", {})

        if not isinstance(raw_match, dict):
            raise ValueError(
                f"applications.{app_name}.match must be a table"
            )

        match = MatchRule(
            window_class=compile_optional_regex(
                raw_match.get("class"),
                field="class",
                app_name=app_name,
            ),
            instance=compile_optional_regex(
                raw_match.get("instance"),
                field="instance",
                app_name=app_name,
            ),
            title=compile_optional_regex(
                raw_match.get("title"),
                field="title",
                app_name=app_name,
            ),
        )

        if not any(
            (
                match.window_class,
                match.instance,
                match.title,
            )
        ):
            raise ValueError(
                f"applications.{app_name}.match must specify at least one "
                f"of: class, instance, title"
            )

        applications[app_name] = Application(
            name=app_name,
            command=tuple(command),
            match=match,
        )

    return Config(
        settle_delay=float(settle_delay),
        applications=applications,
    )


def regex_matches(
    pattern: re.Pattern[str] | None,
    value: str | None,
) -> bool:
    if pattern is None:
        return True

    return value is not None and pattern.search(value) is not None


def matches_application(container: Any, app: Application) -> bool:
    window_properties = container.window_properties or {}

    window_class = window_properties.get("class")
    instance = window_properties.get("instance")
    title = container.name

    return (
        regex_matches(app.match.window_class, window_class)
        and regex_matches(app.match.instance, instance)
        and regex_matches(app.match.title, title)
    )


def is_eligible_window(container: Any) -> bool:
    # leaves() should already restrict this to application containers.
    if container.window is None:
        return False

    # Scratchpad containers commonly report "fresh" or "changed".
    # Normal windows report "none".
    if getattr(container, "scratchpad_state", "none") != "none":
        return False

    workspace = container.workspace()

    if workspace is None:
        return False

    # __i3_scratch is i3's internal scratchpad workspace.
    if workspace.name == "__i3_scratch":
        return False

    return True


class WindowSwitcherDaemon:
    def __init__(self, config: Config, socket_path: Path) -> None:
        self.config = config
        self.socket_path = socket_path

        self.i3: Connection | None = None
        self.server: asyncio.AbstractServer | None = None

        # Most recent first. Container IDs occur at most once.
        self.mru: list[int] = []

        # Only one window can be pending promotion because only one
        # window can hold keyboard focus at a time.
        self.promotion_task: asyncio.Task[None] | None = None
        self.pending_promotion_id: int | None = None

    async def connect_i3(self) -> None:
        self.i3 = await Connection(auto_reconnect=True).connect()

        self.i3.on(Event.WINDOW_FOCUS, self.on_window_focus)
        self.i3.on(Event.WINDOW_CLOSE, self.on_window_close)
        self.i3.on(Event.SHUTDOWN, self.on_i3_shutdown)

    def cancel_pending_promotion(self) -> None:
        if self.promotion_task is not None:
            self.promotion_task.cancel()

        self.promotion_task = None
        self.pending_promotion_id = None

    def on_window_focus(self, _i3: Connection, event: Any) -> None:
        self.cancel_pending_promotion()

        container = event.container

        if not is_eligible_window(container):
            return

        self.pending_promotion_id = container.id
        self.promotion_task = asyncio.create_task(
            self.promote_after_delay(container.id)
        )

    def on_window_close(self, _i3: Connection, event: Any) -> None:
        container_id = event.container.id

        if self.pending_promotion_id == container_id:
            self.cancel_pending_promotion()

        self.remove_from_mru(container_id)

    def on_i3_shutdown(self, _i3: Connection, event: Any) -> None:
        LOG.info("i3 IPC shutdown event: %s", event.change)

        if event.change == "exit":
            asyncio.get_running_loop().stop()

    async def promote_after_delay(self, container_id: int) -> None:
        try:
            await asyncio.sleep(self.config.settle_delay)

            assert self.i3 is not None
            tree = await self.i3.get_tree()
            focused = tree.find_focused()

            if (
                focused is None
                or focused.id != container_id
                or not is_eligible_window(focused)
            ):
                return

            self.promote(container_id)

        except asyncio.CancelledError:
            return

        finally:
            if self.pending_promotion_id == container_id:
                self.pending_promotion_id = None
                self.promotion_task = None

    def remove_from_mru(self, container_id: int) -> None:
        with contextlib.suppress(ValueError):
            self.mru.remove(container_id)

    def promote(self, container_id: int) -> None:
        self.remove_from_mru(container_id)
        self.mru.insert(0, container_id)
        LOG.debug("MRU: %s", self.mru)

    def prune_mru(self, valid_ids: set[int]) -> None:
        self.mru = [
            container_id
            for container_id in self.mru
            if container_id in valid_ids
        ]

    async def launch(self, application: Application) -> None:
        LOG.info(
            "No matching %s window; launching: %r",
            application.name,
            application.command,
        )

        try:
            await asyncio.create_subprocess_exec(
                *application.command,
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            raise RuntimeError(
                f"Failed to launch {application.name}: {error}"
            ) from error

    async def activate(self, app_name: str) -> dict[str, Any]:
        application = self.config.applications.get(app_name)

        if application is None:
            available = ", ".join(sorted(self.config.applications))
            raise ValueError(
                f"Unknown application {app_name!r}. Available: {available}"
            )

        assert self.i3 is not None
        tree = await self.i3.get_tree()
        focused = tree.find_focused()

        all_windows = [
            container
            for container in tree.leaves()
            if is_eligible_window(container)
        ]

        valid_ids = {container.id for container in all_windows}
        self.prune_mru(valid_ids)

        matching_by_id = {
            container.id: container
            for container in all_windows
            if matches_application(container, application)
        }

        if not matching_by_id:
            await self.launch(application)
            return {
                "ok": True,
                "action": "launched",
                "application": app_name,
            }

        # MRU matches come first. Windows never promoted into MRU are
        # appended in stable i3 tree order, ensuring every matching
        # window remains reachable.
        ordered_ids = [
            container_id
            for container_id in self.mru
            if container_id in matching_by_id
        ]

        ordered_id_set = set(ordered_ids)

        ordered_ids.extend(
            container.id
            for container in all_windows
            if (
                container.id in matching_by_id
                and container.id not in ordered_id_set
            )
        )

        focused_id = focused.id if focused is not None else None

        if focused_id in ordered_ids:
            current_index = ordered_ids.index(focused_id)
            target_index = (current_index + 1) % len(ordered_ids)
        else:
            target_index = 0

        target_id = ordered_ids[target_index]
        replies = await self.i3.command(f"[con_id={target_id}] focus")

        failures = [
            reply.error
            for reply in replies
            if not reply.success
        ]

        if failures:
            raise RuntimeError(
                "i3 failed to focus the window: "
                + "; ".join(error or "unknown error" for error in failures)
            )

        return {
            "ok": True,
            "action": "focused",
            "application": app_name,
            "container_id": target_id,
            "position": target_index,
            "window_count": len(ordered_ids),
        }

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw_request = await asyncio.wait_for(
                reader.readline(),
                timeout=2.0,
            )

            if not raw_request:
                return

            request = json.loads(raw_request)
            action = request.get("action")

            if action == "activate":
                result = await self.activate(request["application"])

            elif action == "status":
                result = {
                    "ok": True,
                    "applications": sorted(self.config.applications),
                    "mru": self.mru,
                    "settle_delay": self.config.settle_delay,
                }

            else:
                raise ValueError(f"Unknown action: {action!r}")

        except Exception as error:
            LOG.exception("Client request failed")
            result = {
                "ok": False,
                "error": str(error),
            }

        finally:
            writer.write((json.dumps(result) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    async def prepare_socket(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.socket_path.exists():
            return

        # Avoid replacing a socket belonging to a daemon that is
        # already alive.
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.socket_path
            )
            writer.write(b'{"action":"status"}\n')
            await writer.drain()
            await asyncio.wait_for(reader.readline(), timeout=1.0)
            writer.close()
            await writer.wait_closed()

        except (ConnectionError, OSError, asyncio.TimeoutError):
            self.socket_path.unlink(missing_ok=True)

        else:
            raise RuntimeError(
                f"Another daemon is already listening at {self.socket_path}"
            )

    async def run(self) -> None:
        await self.prepare_socket()
        await self.connect_i3()

        self.server = await asyncio.start_unix_server(
            self.handle_client,
            path=self.socket_path,
        )

        os.chmod(self.socket_path, 0o600)

        LOG.info("Listening on %s", self.socket_path)
        LOG.info("Focus-settle delay: %.3f seconds", self.config.settle_delay)

        try:
            async with self.server:
                await self.server.serve_forever()

        finally:
            self.cancel_pending_promotion()
            self.socket_path.unlink(missing_ok=True)


async def send_request(
    socket_path: Path,
    request: dict[str, Any],
) -> dict[str, Any]:
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except (ConnectionError, OSError) as error:
        raise RuntimeError(
            f"Could not contact i3-window-switcher daemon at "
            f"{socket_path}: {error}"
        ) from error

    try:
        writer.write((json.dumps(request) + "\n").encode())
        await writer.drain()

        raw_response = await asyncio.wait_for(
            reader.readline(),
            timeout=5.0,
        )

        if not raw_response:
            raise RuntimeError("Daemon returned an empty response")

        return json.loads(raw_response)

    finally:
        writer.close()
        await writer.wait_closed()


async def async_main(args: argparse.Namespace) -> int:
    config_path = Path(args.config).expanduser()
    socket_path = runtime_socket_path()

    if args.subcommand == "daemon":
        config = load_config(config_path)
        daemon = WindowSwitcherDaemon(config, socket_path)

        loop = asyncio.get_running_loop()

        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                signum,
                lambda: asyncio.create_task(shutdown_daemon(daemon)),
            )

        await daemon.run()
        return 0

    if args.subcommand == "activate":
        response = await send_request(
            socket_path,
            {
                "action": "activate",
                "application": args.application,
            },
        )

    elif args.subcommand == "status":
        response = await send_request(
            socket_path,
            {"action": "status"},
        )

    else:
        raise RuntimeError(f"Unhandled subcommand: {args.subcommand}")

    if not response.get("ok"):
        print(response.get("error", "Unknown daemon error"), file=sys.stderr)
        return 1

    if args.verbose or args.subcommand == "status":
        print(json.dumps(response, indent=2))

    return 0


async def shutdown_daemon(daemon: WindowSwitcherDaemon) -> None:
    if daemon.server is not None:
        daemon.server.close()
        await daemon.server.wait_closed()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Find, launch, focus, and cycle application windows using "
            "an i3 focus-history daemon."
        )
    )

    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="TOML configuration path",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print client responses and enable daemon debug logging",
    )

    subparsers = parser.add_subparsers(
        dest="subcommand",
        required=True,
    )

    subparsers.add_parser(
        "daemon",
        help="Run the focus-history daemon",
    )

    activate_parser = subparsers.add_parser(
        "activate",
        help="Focus, cycle, or launch an application",
    )

    activate_parser.add_argument(
        "application",
        help="Application name from the TOML configuration",
    )

    subparsers.add_parser(
        "status",
        help="Show daemon status",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        return asyncio.run(async_main(args))

    except KeyboardInterrupt:
        return 130

    except Exception as error:
        LOG.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())