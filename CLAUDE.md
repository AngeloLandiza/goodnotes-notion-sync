# goodnotes-notion-sync

Two one-way imports into one Notion assignments database:

- **Canvas → Notion** (`canvas-import`): a row per Canvas assignment, with due
  dates kept current. Joined to the Courses database on the course code.
- **GoodNotes → Notion** (`sync`): the Drive URL of the PDF whose name matches
  the assignment title, written into a URL property. GoodNotes has no API; the
  hook is its Auto-Backup to Drive.

Neither writes back to its source. Nothing is ever deleted.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                     # 152 tests, offline, ~4s
python -m goodnotes_notion_sync --dry-run  # report without writing
python -m goodnotes_notion_sync            # write links
python -m goodnotes_notion_sync auth       # mint a Google refresh token
python -m goodnotes_notion_sync canvas-import --dry-run
python -m goodnotes_notion_sync canvas-import
```

Always work inside the venv. This machine is macOS with Homebrew Python, so a
bare `pip install` fails with PEP 668 `externally-managed-environment`.
`--break-system-packages` would "work" and is the wrong answer.

No linter or formatter is configured. Tests are stdlib + pytest and never touch
the network. Keep it that way — it is why they run instantly and why the
matching logic is the part that actually gets tested.

## Layout

| File | Role |
|---|---|
| `matching.py` | normalise + score filenames against titles. The only interesting logic. Pure, no I/O. |
| `canvas.py` | read-only Canvas REST: courses, assignments, Link-header pagination |
| `canvas_import.py` | Canvas -> Notion mapping and the create/update/adopt decision |
| `drive.py` | read-only recursive Drive walk, returns `Candidate`s |
| `notion.py` | query a database, create a page, PATCH properties |
| `sync.py` | pairs them up, produces a `Report` |
| `cli.py` | argparse, dotenv, subcommands |
| `oauth.py` | one-time loopback OAuth to mint a refresh token |
| `api/`, `public/`, `vercel.json` | optional Vercel dashboard over the same `run_sync()` |

## Decisions that look arbitrary but are not

Each of these has a test. Undoing one silently breaks matching.

**PDFs only; `.goodnotes` archives are skipped** (`drive.py`). When the backup
format is "both", every notebook lands twice with the same stem. Indexing both
gives two candidates scoring identically, the ambiguity guard fires, and
*nothing* links at all. This is the real setup on the connected Drive.

**Only exact normalised equality scores 1.0** — the `min(0.97, ...)` clamp in
`score()`. Token-set similarity rates a subset as perfect, so `Homework 3`
would tie `CS 411 Homework 3` for the same file and whichever was processed
first would claim it.

**Course code is a veto, not a penalty.** `CS 411 | Homework 3` must never
resolve to a CS 412 notebook, however close the rest reads.

**Course numbers are excluded from the number-conflict check.** Otherwise the
`411` in the title reads as a disagreement with a bare `Homework 3.pdf`.

**Ambiguity refuses rather than guesses.** A wrong link looks right and gets
trusted; a missing link gets reported and fixed.

**Highest score claims a file first** — `sync.py` sorts before assigning, so
assignment order in Notion never decides who wins a contested PDF.

**No rapidfuzz.** stdlib `difflib` keeps the runtime dependency list at
`requests` alone.

**The Canvas import never rewrites a Title after creation.** Renaming a row to
match a GoodNotes notebook is how the PDF gets linked. An import that restored
Canvas' wording every six hours would undo that silently, and the sync report
would blame the notebook. Only `Due Date` and `Canvas URL` are updated.

**Underscores are stripped before `course_code()` reads a Canvas course.** UIC
sends `2026_Fall_CS_411_39421`, and `_` is a word character, so `\b` never
fires in front of `CS`. Without the strip every course parses as codeless and
the entire import lands in the unmatched pile.

**A Canvas course with no Notion match is skipped, not imported.** An
assignment row with no `Course` relation is invisible in every course-filtered
view of the Academic OS: importing it looks like success and behaves like data
loss. `--allow-unmatched-courses` overrides, and the exit code is `3` so a
scheduled run is visibly amber.

**Due dates are converted from UTC to campus time.** Canvas stores 11:59pm
Central as `04:59:59Z` the *next day*. Unconverted, every deadline moves a day
later in the Notion calendar.

**Adoption uses `adoption_key()`, never `matching.normalize()`.** The
filename normaliser drops "notes", "copy" and "for", maps `hw`->`homework` and
roman numerals to digits. Under it a page called "CS 411 Notes for Homework 3"
is indistinguishable from the assignment "CS 411 | Homework 3", so the import
would stamp the Canvas id onto the notes page and never create the row.
`adoption_key` flattens case, accents and punctuation and drops nothing.

**An adopted row is claimed inside the run, not just in Notion.** `existing` is
a snapshot read once. Without `adoptee.canvas_id = ...` plus the `by_title`
eviction, two Canvas assignments with the same name both adopt the same page:
two writes, an arbitrary winner, no row at all for the loser, and a duplicate
for it next run.

**One unreadable Canvas course does not abort the import.** Rows for earlier
courses are already written by then; letting `CanvasError` escape throws away
the report that says what those writes were. Failures are collected per course
and printed.

**Every property name is overridable, and "" means don't write it.** Notion's
own default title property is "Name". Hardcoding "Title", "Type" and "Status"
made the first `create_page` 400 on any database shaped even slightly
differently.

**403 from Canvas can mean rate limit.** Canvas throttles with
`403 Forbidden (Rate Limit Exceeded)`, not 429. Treated as permanent it aborts
a large run mid-write, with the wrong explanation.

**`points_possible` is not imported.** It is a point total, not a share of the
final mark. Writing it into `Weight` would corrupt the grade rollups that the
Courses database computes from it.

**Tokens are compared as bytes.** `hmac.compare_digest` raises `TypeError`
on a non-ASCII `str`, and `authorised()` runs *outside* the handler's try
block -- so one curl with an accent in the token crashed the function instead
of getting a 401.

**`CANVAS_TOKEN` is not in `api/_shared.REQUIRED`.** Canvas is the optional
half; adding it there would 500 every deployment that only wants the GoodNotes
sync. `/api/canvas` answers `200 {"configured": false}` instead.

**Loopback OAuth, not the device flow** (`oauth.py`). Google restricts the
device flow to `drive.appdata` and `drive.file` for Drive; `drive.file` only
sees files the app created, so it cannot read a backup folder, and
`drive.readonly` is not offered there at all. The client must be a **Desktop
app**; a `TVs and Limited Input devices` client fails with `invalid_client`.
PKCE S256 is included because installed apps cannot keep a secret.
`access_type=offline` **and** `prompt=consent` are both required or Google
returns no refresh token on re-authorisation.

## Vercel dashboard (optional surface)

`api/` + `public/` + `vercel.json` deploy a token-gated web UI over the same
`run_sync()`. `api/_shared.py::authorised` accepts `APP_TOKEN` (dashboard) or
`CRON_SECRET` (Vercel's scheduler) and **fails closed** when neither is set —
that endpoint is publicly reachable, so an unset token must never mean open.
`tests/test_api_auth.py` pins this along with ten other cases.

Hobby cron is capped at once per day and a more frequent expression fails at
deploy time, so GitHub Actions stays the real scheduler. Vercel is there for
the report and the manual trigger.

`CRON_SECRET` is **not** auto-generated by Vercel, contrary to what its name
suggests: you create the env var and Vercel sends its value as the
`Authorization` header. Deployment needs eight variables — the CLI's six, plus
`APP_TOKEN` and `CRON_SECRET`.

Two deployment settings that are easy to get wrong:

- **Application Preset must be `Other`.** A detected Python framework preset
  takes precedence over file-based functions — `/api/*.py` would stop being
  routed entirely, and the preset expects an `app.py` ASGI/WSGI entrypoint this
  project has no reason to have.
- **`api/_shared.py` keeps its underscore.** Vercel turns every `.py` in `/api`
  into a function except those starting with `_` or `.`. Rename it and the
  build tries to publish a module with no handler.

## Live environment

- Drive backup folder: `1Y0qfNKgm0xHfMm8LegQ0sjTDL4RO-p56` — flat, currently
  `AI Rule Builder` and `LC`, each present as both `.pdf` and `.goodnotes`
- Notion assignments database: `2e621472-ffe4-81e1-93ad-d47837eb7491`
  (data source `collection://2e621472-ffe4-8107-8fca-000b9ecaf21e`)
- Notion courses database: `2e621472ffe481978e95ec76dd6f2879`
  (data source `collection://2e621472-ffe4-8111-b7a2-000badfeeb44`)
- Properties written: `Notes PDF` (URL) by the sync; `Canvas ID` (text),
  `Canvas URL` (url), `Due Date`, `Course`, `Type`, `Status` by the import.
  A `Notes` formula renders the PDF link as a compact marker in table views.
- Canvas: `https://canvas.uic.edu`. UIC is mid-migration from Blackboard --
  Fall 2026 is each instructor's choice, Canvas is mandatory from Spring 2027 --
  so some of the six courses will have no Canvas presence at all. The report
  distinguishes "no Notion match" from "no assignments found".
- The assignments database is currently **empty**, so a live sync links nothing
  until the Canvas import (or a person) creates rows.

## Ideas not yet built

- Import Canvas announcements or files (deliberately out of scope for now)
- Mark a row `Submitted` from the Canvas submission state
- Reverse direction: create a Notion row from an orphaned PDF
- Second property linking the `.goodnotes` archive (the editable copy)
- Match against a Lectures database as well as Assignments
- Cache the Drive listing between runs
- `--interactive` to confirm borderline matches instead of skipping them
