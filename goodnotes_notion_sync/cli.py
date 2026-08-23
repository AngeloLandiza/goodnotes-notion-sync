"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys

import requests

from .drive import SCOPES, DriveClient, DriveError
from .notion import NotionClient, NotionError
from .sync import run_sync

DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _load_dotenv(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing {name}. Put it in .env (local) or repository secrets (Actions)."
        )
    return value


def cmd_auth(args: argparse.Namespace) -> int:
    """One-time device-code flow to mint a Google refresh token."""
    client_id = _require("GOOGLE_CLIENT_ID")
    client_secret = _require("GOOGLE_CLIENT_SECRET")

    start = requests.post(
        DEVICE_CODE_URL,
        data={"client_id": client_id, "scope": " ".join(SCOPES)},
        timeout=30,
    )
    if start.status_code != 200:
        print(f"Could not start device flow: {start.text[:300]}", file=sys.stderr)
        return 1
    payload = start.json()

    print()
    print("  1. Open:", payload["verification_url"])
    print("  2. Enter code:", payload["user_code"])
    print()
    print("Waiting for you to finish in the browser...")

    import time

    interval = int(payload.get("interval", 5))
    deadline = time.time() + int(payload.get("expires_in", 600))
    while time.time() < deadline:
        time.sleep(interval)
        poll = requests.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "device_code": payload["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=30,
        )
        data = poll.json()
        if poll.status_code == 200:
            print()
            print("Success. Save this as GOOGLE_REFRESH_TOKEN:")
            print()
            print("   ", data["refresh_token"])
            print()
            return 0
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        print(f"Auth failed: {data}", file=sys.stderr)
        return 1

    print("Timed out waiting for authorisation.", file=sys.stderr)
    return 1


def cmd_sync(args: argparse.Namespace) -> int:
    notion = NotionClient(_require("NOTION_TOKEN"))
    drive = DriveClient(
        _require("GOOGLE_CLIENT_ID"),
        _require("GOOGLE_CLIENT_SECRET"),
        _require("GOOGLE_REFRESH_TOKEN"),
    )

    folder = args.folder or _require("GDRIVE_FOLDER_ID")
    folder_id = drive.resolve_folder(folder)

    report = run_sync(
        notion=notion,
        drive=drive,
        database_id=args.database or _require("NOTION_ASSIGNMENTS_DB"),
        folder_id=folder_id,
        url_property=args.url_property,
        title_property=args.title_property,
        threshold=args.threshold,
        margin=args.margin,
        dry_run=args.dry_run,
        force=args.force,
    )

    text = report.to_text(dry_run=args.dry_run)
    print(text)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("## GoodNotes sync\n\n```\n" + text + "\n```\n")

    return 0


def build_parser() -> argparse.ArgumentParser:
    # Shared flags live on a parent parser attached to each subcommand, so they
    # work in either position without the subparser's default clobbering a
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file",
        default=".env",
        help="dotenv file to read before running (default: .env)",
    )
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(
        prog="goodnotes-notion-sync",
        description=(
            "Link each Notion assignment to the GoodNotes PDF of the same name "
            "in your Google Drive backup folder. Runs 'sync' when no "
            "subcommand is given."
        ),
    )

    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser(
        "auth",
        parents=[common],
        help="mint a Google refresh token (run once)",
    )
    auth.set_defaults(func=cmd_auth)

    sync = sub.add_parser(
        "sync", parents=[common], help="match PDFs to assignments (default)"
    )
    sync.add_argument("--database", help="Notion assignments database id")
    sync.add_argument("--folder", help="Drive folder id, URL, or exact name")
    sync.add_argument("--url-property", default="Notes PDF")
    sync.add_argument("--title-property", default="Title")
    sync.add_argument(
        "--threshold",
        type=float,
        default=0.78,
        help="minimum similarity to accept a match (default: 0.78)",
    )
    sync.add_argument(
        "--margin",
        type=float,
        default=0.06,
        help="how far ahead of runner-up the winner must be (default: 0.06)",
    )
    sync.add_argument(
        "-n", "--dry-run", action="store_true", help="report without writing to Notion"
    )
    sync.add_argument(
        "-f", "--force", action="store_true", help="re-link assignments that have a URL"
    )
    sync.set_defaults(func=cmd_sync)

    return parser


SUBCOMMANDS = ("auth", "sync")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # A bare invocation, or one with only flags, means "sync" -- but never
    # swallow a top-level help request into the subparser.
    wants_help = bool({"-h", "--help"} & set(argv))
    has_subcommand = any(arg in SUBCOMMANDS for arg in argv)
    if not has_subcommand and not wants_help:
        argv = ["sync", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    _load_dotenv(pathlib.Path(args.env_file))

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    try:
        return args.func(args)
    except (DriveError, NotionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
