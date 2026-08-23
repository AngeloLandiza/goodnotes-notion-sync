# goodnotes-notion-sync

Links each assignment row in a Notion database to the GoodNotes PDF of the same
name in a Google Drive backup folder, by writing the Drive URL into a URL
property. GoodNotes has no API; the hook is its Auto-Backup to Drive.

## Commands

```bash
pip install -r requirements-dev.txt
pytest                                     # 36 tests, all offline, ~0.15s
python -m goodnotes_notion_sync --dry-run  # report without writing
python -m goodnotes_notion_sync            # write links
python -m goodnotes_notion_sync auth       # mint a Google refresh token
```

No linter or formatter is configured. Tests are stdlib + pytest and never touch
the network. Keep it that way — it is why they run instantly and why the
matching logic is the part that actually gets tested.

## Layout

| File | Role |
|---|---|
| `matching.py` | normalise + score filenames against titles. The only interesting logic. Pure, no I/O. |
| `drive.py` | read-only recursive Drive walk, returns `Candidate`s |
| `notion.py` | two REST calls: query a database, PATCH a URL property |
| `sync.py` | pairs them up, produces a `Report` |
| `cli.py` | argparse, dotenv, device-code OAuth |

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

## Live environment

- Drive backup folder: `1Y0qfNKgm0xHfMm8LegQ0sjTDL4RO-p56` — flat, currently
  `AI Rule Builder` and `LC`, each present as both `.pdf` and `.goodnotes`
- Notion assignments database: `2e621472-ffe4-81e1-93ad-d47837eb7491`
- Property written: `Notes PDF` (URL). A `Notes` formula renders it as a
  compact marker in table views.
- The assignments database is currently **empty**, so a live run links nothing
  until rows exist. Use the stub-client tests to exercise the loop meanwhile.

## Ideas not yet built

- Reverse direction: create a Notion row from an orphaned PDF
- Second property linking the `.goodnotes` archive (the editable copy)
- Match against a Lectures database as well as Assignments
- Cache the Drive listing between runs
- `--interactive` to confirm borderline matches instead of skipping them
