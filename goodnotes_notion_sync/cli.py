"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys


from .drive import SCOPES, DriveClient, DriveError
from .notion import NotionClient, NotionError
from .oauth import OAuthError, run_local_flow
from .sync import run_sync


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
    """One-time browser authorisation that prints a refresh token."""
    try:
        token = run_local_flow(
            client_id=_require("GOOGLE_CLIENT_ID"),
            client_secret=_require("GOOGLE_CLIENT_SECRET"),
            scopes=SCOPES,
            open_browser=not args.no_browser,
        )
    except OAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Success. Save this as GOOGLE_REFRESH_TOKEN:\n")
    print(f"    {token}\n")
    return 0


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
    auth.add_argument(
        "--no-browser",
        action="store_true",
        help="print the URL instead of opening a browser",
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
    except (DriveError, NotionError, OAuthError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
