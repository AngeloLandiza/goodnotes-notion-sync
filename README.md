# goodnotes-notion-sync

Link every assignment in a Notion database to the GoodNotes PDF of the same
name in your Google Drive backup folder.

GoodNotes has no public API. What it does have is **Auto-Backup to Google
Drive**, which drops one PDF per notebook into a folder tree that mirrors your
GoodNotes folders. This tool reads that folder, matches filenames against your
Notion assignment titles, and writes the Drive link into a URL property.

Name a notebook `CS 411 | Homework 3` and it lands on the `CS 411 | Homework 3`
row in Notion. One click from the assignment to your handwriting.

```
36 assignment(s), 41 PDF(s) in Drive

Linked (4):
  1.00  CS 411 | Homework 3
         -> CS 411/CS 411 Homework 3.pdf
  0.94  STAT 382 | Quiz 2
         -> STAT 382/Quiz 2.pdf

No match (1):
  IDS 435 | Project Proposal
         best candidate 'IDS 435 Project.pdf' scored 0.71 < 0.78

PDFs with no assignment (2) - rename the notebook to match if one of these
should be linked:
  Scratch/Untitled Notebook.pdf
```

## How the matching works

Filenames and titles are normalised before comparison: case, accents,
punctuation and separators are flattened; `.pdf` and Drive's ` (1)` duplicate
suffix are stripped; `HW 4` becomes `homework 4`; `Problem Set 07` becomes
`problemset 7`; roman numerals become digits.

Three rules stop it from writing a link that merely looks right:

| Rule | Effect |
|---|---|
| **Course-code veto** | `CS 411 | Homework 3` can never match a `CS 412` file, however similar the rest reads |
| **Number disagreement** | `Homework 3` vs `Homework 4` is penalised hard enough to fall below threshold. Course numbers are excluded, so `CS 411 \| Homework 3` still matches `Homework 3.pdf` |
| **Ambiguity refusal** | If the top two candidates are within `--margin` of each other, nothing is written. A wrong link is worse than no link |

A PDF is claimed by at most one assignment, highest-confidence first, so an
exact title always beats a partial one for the same file.

Anything not matched is listed in the report, in both directions — assignments
with no PDF, and PDFs with no assignment. That list is the thing to read: it
tells you exactly which notebook to rename.

## Setup

### 1. Turn on the GoodNotes backup

In GoodNotes: **Settings → Auto-Backup → Google Drive**, and pick (or create)
a destination folder. Note the folder's name, id, or URL.

Format can be **PDF** or **both**. If you pick both, every notebook lands twice
-- `Notebook.pdf` and `Notebook.goodnotes` -- and this tool indexes only the
PDF. That is on purpose: the two files share a stem and would score
identically, which would trip the ambiguity guard and stop *anything* from
linking. The PDF is also the only one of the pair that previews in Drive and
Notion.

### 2. Add the Notion property

In your assignments database add a **URL** property called `Notes PDF`
(any name works — pass `--url-property` if you use another).

Create an internal integration at
<https://www.notion.com/my-integrations>, copy the token, then in Notion open
the assignments database → **⋯ → Connections → Connect to** your integration.
Without that last step the API cannot see the database.

### 3. Google credentials

In [Google Cloud Console](https://console.cloud.google.com/): create a project,
enable the **Google Drive API**, then **Credentials → Create credentials →
OAuth client ID → TVs and Limited Input devices**. Copy the client ID and
secret.

Read-only Drive scope is all this asks for.

### 4. Configure and authorise

```bash
git clone https://github.com/<you>/goodnotes-notion-sync
cd goodnotes-notion-sync
pip install -r requirements.txt
cp .env.example .env      # fill in the values you just collected

python -m goodnotes_notion_sync auth   # prints a refresh token
# paste it into .env as GOOGLE_REFRESH_TOKEN
```

### 5. Run it

```bash
python -m goodnotes_notion_sync --dry-run   # see what it would do
python -m goodnotes_notion_sync             # write the links
```

## Options

```
--database ID          Notion assignments database (default: $NOTION_ASSIGNMENTS_DB)
--folder ID|URL|NAME   Drive backup folder (default: $GDRIVE_FOLDER_ID)
--url-property NAME    Notion URL property to write (default: "Notes PDF")
--title-property NAME  Notion title property to read (default: "Title")
--threshold 0.78       minimum similarity to accept a match
--margin 0.06          how far ahead of the runner-up the winner must be
-n, --dry-run          report without writing
-f, --force            re-link assignments that already have a URL
-v, --verbose
```

Raise `--threshold` if you get wrong matches; lower it if good ones are being
missed. `--dry-run` first, always.

## Running it on a schedule

`.github/workflows/sync.yml` runs the sync every 6 hours and on demand. Add
these repository secrets (**Settings → Secrets and variables → Actions**):

`NOTION_TOKEN`, `NOTION_ASSIGNMENTS_DB`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID`

Each run writes its report to the job summary, so the unmatched list is one
click from the Actions tab.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The matching rules are the part worth testing — `tests/test_matching.py`
covers normalisation, the course veto, number conflicts and ambiguity;
`tests/test_sync.py` covers the sync loop against stub clients.

## Limits

- Matching is on **filename only**. The PDF contents are never read or
  downloaded.
- GoodNotes' backup is one-way and runs on its own schedule, so a brand new
  notebook may take a few minutes to appear in Drive.
- Renaming a notebook creates a *new* PDF in Drive; the old one stays and shows
  up as an orphan until you delete it.

## Licence

MIT
