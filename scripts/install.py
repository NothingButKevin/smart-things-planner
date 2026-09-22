#!/usr/bin/env python3
"""Install the shared skill and local MCP servers for Codex and Claude Code."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / "bin" / "python"
SKILL = ROOT / "skills" / "mail-to-things"
THINGS_SERVER = ROOT / "mcp" / "things_server.py"
GMAIL_SERVER = ROOT / "mcp" / "gmail_server.py"


def command_path(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if name == "codex":
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.exists():
            return str(bundled)
    if name == "claude":
        local = Path.home() / ".local" / "bin" / "claude"
        if local.exists():
            return str(local)
    return None


def gws_path() -> str | None:
    candidates = [
        shutil.which("gws"),
        "/opt/homebrew/bin/gws",
        "/usr/local/bin/gws",
        str(Path.home() / ".local" / "bin" / "gws"),
    ]
    return next((item for item in candidates if item and Path(item).exists()), None)


def compatible_python() -> str | None:
    candidates = [
        sys.executable,
        *(shutil.which(name) for name in ("python3.13", "python3.12", "python3.11", "python3.10")),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ]
    for candidate in dict.fromkeys(item for item in candidates if item):
        result = subprocess.run(
            [candidate, "-c", "import sys; raise SystemExit(sys.version_info < (3, 10))"],
            check=False,
        )
        if result.returncode == 0:
            return candidate
    return None


def run(command: list[str], *, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess[str] | None:
    print("+", " ".join(command))
    if dry_run:
        return None
    return subprocess.run(command, text=True, check=check, capture_output=not check)


def install_environment(dry_run: bool) -> None:
    if not PYTHON.exists():
        base_python = compatible_python()
        if not base_python:
            raise RuntimeError("Python 3.10 or newer was not found")
        run([base_python, "-m", "venv", str(VENV)], dry_run=dry_run)
    run(
        [str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
        dry_run=dry_run,
    )


def install_skill(client: str, *, force: bool, dry_run: bool) -> None:
    destination = Path.home() / f".{client}" / "skills" / "mail-to-things"
    if destination.is_symlink() and destination.resolve() == SKILL.resolve():
        print(f"Skill already linked for {client}: {destination}")
        return
    if destination.exists() or destination.is_symlink():
        if not force:
            raise RuntimeError(
                f"{destination} already exists. Re-run with --force to replace it."
            )
        print(f"Replacing existing skill path: {destination}")
        if not dry_run:
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()
    print(f"+ link {destination} -> {SKILL}")
    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(SKILL, target_is_directory=True)


def mcp_exists(executable: str, name: str) -> bool:
    result = subprocess.run(
        [executable, "mcp", "get", name], text=True, capture_output=True, check=False
    )
    return result.returncode == 0


def configure_server(
    client: str,
    executable: str,
    name: str,
    server: Path,
    environment: dict[str, str],
    *,
    force: bool,
    dry_run: bool,
) -> None:
    exists = False if dry_run else mcp_exists(executable, name)
    if exists and not force:
        print(f"Keeping existing {client} MCP server: {name}")
        return
    if exists:
        remove = [executable, "mcp", "remove"]
        if client == "claude":
            remove += ["-s", "user"]
        remove.append(name)
        run(remove, dry_run=dry_run)

    add = [executable, "mcp", "add"]
    if client == "claude":
        add += ["-s", "user", name]
        for key, value in environment.items():
            add += ["-e", f"{key}={value}"]
    else:
        for key, value in environment.items():
            add += ["--env", f"{key}={value}"]
        add.append(name)
    add += ["--", str(PYTHON), str(server)]
    run(add, dry_run=dry_run)


def parse_profile(value: str) -> tuple[str, Path]:
    alias, separator, directory = value.partition("=")
    alias = alias.strip()
    if not separator or not alias or not directory.strip():
        raise argparse.ArgumentTypeError("use ALIAS=CONFIG_DIRECTORY")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in alias):
        raise argparse.ArgumentTypeError("alias may contain only letters, numbers, - and _")
    return alias, Path(directory).expanduser().resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install Smart Things Planner for Codex and/or Claude Code."
    )
    parser.add_argument(
        "--client", choices=["both", "codex", "claude"], default="both"
    )
    parser.add_argument(
        "--gmail-profile",
        action="append",
        default=[],
        type=parse_profile,
        metavar="ALIAS=CONFIG_DIRECTORY",
        help="Add one read-only Gmail account. Repeat for multiple accounts.",
    )
    parser.add_argument("--force", action="store_true", help="Replace matching skill links and MCP entries.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing anything.")
    args = parser.parse_args()

    if sys.platform != "darwin":
        parser.error("Things 3 automation requires macOS")
    gws = gws_path()
    if not gws and args.gmail_profile:
        parser.error("gws was not found in PATH; install Google Workspace CLI first")

    clients = ["codex", "claude"] if args.client == "both" else [args.client]
    executables: dict[str, str] = {}
    for client in clients:
        executable = command_path(client)
        if not executable:
            parser.error(f"{client} CLI was not found")
        executables[client] = executable

    install_environment(args.dry_run)
    for client in clients:
        install_skill(client, force=args.force, dry_run=args.dry_run)
        configure_server(
            client,
            executables[client],
            "things3",
            THINGS_SERVER,
            {},
            force=args.force,
            dry_run=args.dry_run,
        )
        for alias, directory in args.gmail_profile:
            if not args.dry_run:
                directory.mkdir(parents=True, exist_ok=True)
            configure_server(
                client,
                executables[client],
                f"gmail-{alias}",
                GMAIL_SERVER,
                {
                    "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": str(directory),
                    "GMAIL_ACCOUNT_ALIAS": alias,
                    "GWS_COMMAND": str(gws),
                },
                force=args.force,
                dry_run=args.dry_run,
            )

    print("\nInstalled. Restart Codex / Claude Code so they reload the skill and MCP servers.")
    if args.gmail_profile:
        print("Authorize each Gmail profile with the read-only commands shown in the README.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
