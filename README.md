# goodnotes-notion-sync

Keep a Notion assignments database in step with the two places coursework
actually lives: **Canvas** (what is due, and when) and **GoodNotes** (what you
wrote about it).

- `canvas-import` creates a Notion row for every Canvas assignment and keeps
  due dates current.
- `sync` links each of those rows to the GoodNotes PDF of the same name in your
  Google Drive backup folder.

Both are optional halves. Run either on its own.

It deploys three ways: a CLI, a scheduled GitHub Action, and an optional Vercel
dashboard — which itself runs either as a single-user tool configured by
environment variables, or as a small multi-account app where people sign in
with Google and connect their own Notion, Canvas and Drive.

## GoodNotes → Notion

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
OAuth client ID → Desktop app**. Copy the client ID and secret.

> **It must be `Desktop app`.** Google's device flow — the one that shows a
> code to type into another screen — is restricted to a short scope list, and
> for Drive that is only `drive.appdata` and `drive.file`. `drive.file` sees
> only files the app itself created, so it can never read your GoodNotes
> folder. `auth` therefore uses the installed-app loopback flow, which supports
> `drive.readonly`; a `TVs and Limited Input devices` client fails it with
> `invalid_client: Invalid client type`.

Read-only Drive scope is all this asks for. `auth` opens your browser, listens
on a throwaway `127.0.0.1` port for the redirect, and exchanges the code with
PKCE. Nothing needs registering as a redirect URI — Desktop app clients accept
any loopback port.

> **Publish the consent screen, or your token dies weekly.** Google issues
> refresh tokens that **expire after 7 days** to any app whose OAuth consent
> screen is in `Testing` status with an external user type. The symptom is a
> sync that works for a week and then fails with `invalid_grant`. Fix it in
> Google Cloud Console → *OAuth consent screen* → **Publish app**. You will
> see an "unverified app" warning when authorising — that is expected for a
> restricted scope like `drive.readonly`, and clicking through it is fine for
> your own account.

### 4. Configure and authorise

```bash
git clone https://github.com/<you>/goodnotes-notion-sync
cd goodnotes-notion-sync

python3 -m venv .venv          # see the note below if you skip this
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env           # fill in the values you just collected
python -m goodnotes_notion_sync auth   # prints a refresh token
# paste it into .env as GOOGLE_REFRESH_TOKEN
```

> **Use the virtualenv.** On macOS with Homebrew Python (and most current Linux
> distros) a bare `pip install` fails with `error: externally-managed-environment`
> — PEP 668 stops you installing into the system interpreter. A venv is the fix;
> `--break-system-packages` is not.

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

## Canvas → Notion

```bash
python -m goodnotes_notion_sync canvas-import --dry-run
python -m goodnotes_notion_sync canvas-import
```

```
6 Canvas course(s), 4 matched to Notion, 37 assignment(s) seen

Created (12):
  CS 411 | Homework 3
         2026-09-13 23:59
  STAT 382 | Quiz 2
         2026-09-16 13:00

Updated (1):
  CS 412 | Project Milestone 1
         2026-10-09 23:59  [was 2026-10-02 23:59]

Already up to date (24)

Canvas courses with no Notion match (2) - set the Code property on the Notion
course row to fix:
  Advanced Business Data Mining
         no Notion course has Code 'IDS 435'
  Notion knows: CS 342, CS 411, CS 412, STAT 382
```

### The join is the course code

Canvas calls a course `2026_Fall_CS_411_39421`. Notion calls it a row whose
**Code** property reads `CS 411`. Lining those up is the whole integration, and
it is also the only thing you have to set up by hand: fill in `Code` on each
row of your Courses database.

A Canvas course with no Notion match is **reported and skipped**, not imported.
An assignment with no Course relation is invisible in every course-filtered
view in a Notion academic template, so importing it would look like success and
behave like data loss. Pass `--allow-unmatched-courses` if you want them anyway.

### What it writes, and what it refuses to touch

| Property | On create | On later runs |
|---|---|---|
| `Title` | `CS 411 \| Homework 3` | **never rewritten** |
| `Due Date` | from Canvas, in campus time | updated when Canvas moves it |
| `Canvas URL` | link to the assignment | refreshed |
| `Canvas ID` | Canvas' numeric id | the dedupe key |
| `Course` | relation, matched by code | left alone |
| `Type` | inferred from the name | left alone |
| `Status` | `Not started` | **left alone** |

Titles are written exactly once. Renaming a row to match a GoodNotes notebook
is a deliberate act — it is how the PDF gets linked — and an import that
restored Canvas' wording every six hours would quietly undo it, then blame the
notebook in the sync report.

Grades and weights are not imported at all. Canvas' `points_possible` is a
point total, not a share of your final mark, and writing it into a `Weight`
column would silently corrupt the grade formulas in an academic template.

Due dates are converted from Canvas' UTC into campus time. An 11:59pm Central
deadline is stored by Canvas as `04:59:59Z` **the next day**; copied across
unconverted, every deadline lands a day late in Notion's calendar.

### Setup

1. In Canvas: **Account → Settings → New Access Token**. Copy it — it is shown
   once. Treat it as a password; it can read everything your account can. This
   tool never writes to Canvas.
2. Add `CANVAS_TOKEN`, `CANVAS_BASE_URL` and `NOTION_COURSES_DB` to `.env`.
3. Add a **Canvas ID** (text) and **Canvas URL** (url) property to your Notion
   assignments database. `Type` (select) and `Status` (status) are written too
   if you have them — pass `--type-property ""` / `--status-property ""` if you
   do not.
4. Fill in the **Code** property on each row of your Courses database.

```
--database ID              Notion assignments database
--courses-database ID      Notion courses database
--canvas-url HOST          your school's Canvas (default: $CANVAS_BASE_URL)
--timezone NAME            campus timezone (default: America/Chicago)
--enrollment-state STATE   active | completed | invited_or_pending
--skip-undated             ignore assignments with no due date
--allow-unmatched-courses  import courses that have no Notion row
--title-property NAME      title property (default "Title"; Notion's own
                           default is "Name")
--type-property NAME       select property for the inferred kind; "" to skip
--status-property NAME     status set on new rows; "" to skip
--new-status NAME          status given to new rows (default "Not started")
-n, --dry-run
```

Exit code `3` means some Canvas course had no Notion match — the run still
imported everything it could.

> **Not every course is on Canvas.** UIC is mid-migration from Blackboard:
> through Fall 2026 each instructor picks one, and Canvas only becomes
> mandatory in Spring 2027. Expect the import to cover some of your courses and
> report the rest as having nothing to read.

## Running it on a schedule

`.github/workflows/sync.yml` runs every 6 hours and on demand. Add these
repository secrets (**Settings → Secrets and variables → Actions**):

`NOTION_TOKEN`, `NOTION_ASSIGNMENTS_DB`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID`

Add `CANVAS_TOKEN`, `CANVAS_BASE_URL` and `NOTION_COURSES_DB` as well to turn
on the Canvas step. Without `CANVAS_TOKEN` that step is skipped and the
GoodNotes sync runs alone, so an existing setup keeps working untouched.

Canvas runs **first**. Rows it creates are then visible to the GoodNotes
matcher in the same run, so a new assignment and its notebook link up without
waiting for the next pass.

Each run writes both reports to the job summary, so the unmatched list is one
click from the Actions tab.

## Optional: Vercel dashboard

`vercel.json`, `api/` and `public/` add a web UI over the same code. It runs in
one of two modes, and which one you get depends on a single environment
variable.

### Single-user mode (no database)

The original design. Credentials come from environment variables, the dashboard
is gated by `APP_TOKEN`, and everything runs as you.

```bash
vercel                    # deploy
```

**Set Application Preset to `Other`, not `Python`.** A Python *framework*
preset takes precedence over file-based functions: when one is detected, the
framework app handles every request and files under `/api` never become
functions at all. `Other` gives the zero-config behaviour this layout needs —
each `api/*.py` becomes a function, and `public/` is served statically at `/`.

Files in `/api` starting with `_` are skipped by Vercel, which is why the two
helper modules are named `_shared.py` and `_app.py`.

Set the six variables the CLI uses, plus `APP_TOKEN` (the dashboard login) and
`CRON_SECRET` (what Vercel sends on scheduled runs). Generate both with
`openssl rand -base64 32`. Vercel does **not** create `CRON_SECRET` for you,
contrary to what the name suggests.

Add `CANVAS_TOKEN` and `NOTION_COURSES_DB` to light up the Canvas tab. Without
them `/api/canvas` answers `200` with `configured: false` — deliberately not an
error, so a deployment that only wants the GoodNotes half is never made to look
broken.

> **That endpoint is public.** Every request must carry
> `Authorization: Bearer <APP_TOKEN>` or `<CRON_SECRET>`. With neither variable
> set the handler rejects everything rather than running open.

### Accounts mode (with a database)

Set `DATABASE_URL` and the same deployment becomes multi-user: people sign in
with Google, connect their own Notion, Canvas and Drive, and each gets their
own sync. Nothing is shared between accounts, and you never see anyone else's
credentials.

Single-user mode is not deprecated by this — with `DATABASE_URL` unset the app
behaves exactly as it did before accounts existed.

#### 1. A database

Vercel Postgres no longer exists; it moved to the Marketplace in December 2024
and existing databases were migrated to Neon. Install any Postgres integration
from **Vercel → Storage → Marketplace** (Neon is the default, and its free tier
is ample here). `DATABASE_URL` is injected into the project for you.

Use the **pooled** connection string. Functions open a connection per request,
and a direct endpoint runs out of them quickly.

#### 2. Secrets

```bash
python -m goodnotes_notion_sync keygen
```

That prints `APP_ENCRYPTION_KEY` and `SESSION_SECRET`. Add both in Vercel, plus:

| Variable | What it is |
|---|---|
| `APP_BASE_URL` | `https://your-app.vercel.app` — must be stable and exact |
| `OWNER_EMAIL` | the first address allowed to sign in |
| `GOOGLE_CLIENT_ID` / `_SECRET` | a **Web application** OAuth client |
| `NOTION_OAUTH_CLIENT_ID` / `_SECRET` | a **public** Notion integration |

`APP_BASE_URL` cannot be a preview deployment URL. Those change on every push,
and an OAuth redirect URI has to be registered ahead of time and matched
exactly.

Every stored credential is encrypted with `APP_ENCRYPTION_KEY` before it
reaches Postgres. Lose that key and everyone has to reconnect; to rotate it,
move the old value to `APP_ENCRYPTION_KEY_OLD` and old rows keep decrypting
while new ones are written with the new key.

#### 3. OAuth clients

**Google** — the desktop client used by the CLI will not work for the web flow.
Create a second one: *Credentials → Create credentials → OAuth client ID →
**Web application***, with authorised redirect URI:

```
https://your-app.vercel.app/api/auth/google/callback
```

Scopes are `openid email profile` plus `drive.readonly`, requested together, so
signing in *is* connecting Drive. That removes the `auth` CLI step entirely for
web users.

**Notion** — at <https://www.notion.com/my-integrations>, create (or convert)
an integration and set its type to **Public**, with redirect URI:

```
https://your-app.vercel.app/api/auth/notion/callback
```

A public integration is what turns "create an internal integration, copy the
token, then share each database with it" into one button. Users pick which
pages to share in Notion's own picker, and the dashboard lists exactly those
databases instead of asking for 32-hex ids.

#### 4. Create the schema

```bash
export DATABASE_URL=... APP_ENCRYPTION_KEY=...
python -m goodnotes_notion_sync db-migrate --owner you@example.com
```

Migrations are idempotent and safe to re-run. Then open the app and sign in;
invite people from the **People** tab, or from the CLI:

```bash
python -m goodnotes_notion_sync invite --add classmate@uic.edu
python -m goodnotes_notion_sync sync-all --dry-run
```

`sync-all` is what the scheduler runs. One account failing never stops the
others, and an account that has not finished setting up is skipped quietly
rather than reported as broken every night.

### Before you invite anyone: the Google verification cliff

`drive.readonly` is one of Google's **restricted** scopes, and that has
consequences you cannot design around.

- **An app in `Testing` status issues refresh tokens that expire after seven
  days.** If your consent screen is still in Testing, every stored Drive token
  dies weekly and the sync starts failing with `invalid_grant`. Publishing the
  app to *In production* removes the expiry and costs nothing. **This applies
  to single-user mode too.**
- **Unverified apps requesting a restricted scope show a warning screen** —
  "Google hasn't verified this app" — that each person must click through, and
  are capped at **100 users over the project's entire lifetime**. That cap
  cannot be reset.
- **Real verification requires an annual third-party CASA security
  assessment.** That is not a realistic bar for a student project.

So: publish to production, keep the invite list short, and expect the warning
screen. The invite list is not just convenience — it is what keeps the app
inside the unverified allowance.

### What you are taking on

Inviting someone means holding their Notion token, their Canvas token and their
Google refresh token. A Canvas personal access token can read everything their
Canvas account can. They are encrypted at rest and never rendered back to any
page, but the honest summary is that you become responsible for them. Invite
people who know that is what they are agreeing to.

## Development

```bash
source .venv/bin/activate     # created during setup, above
pip install -r requirements-dev.txt
pytest                        # 257 tests; the storage ones skip unless
                              # TEST_DATABASE_URL points at a Postgres
```

The split between suites is deliberate:

- `tests/test_matching.py` — normalisation, the course veto, number conflicts,
  ambiguity refusal. The rules that decide whether a link is right.
- `tests/test_sync.py` — the sync loop against stub Drive/Notion clients, so
  claim-ordering and dry-run behaviour are covered without a network.
- `tests/test_api_auth.py` — the Vercel endpoint's bearer check, including that
  it **fails closed** when no token is configured.
- `tests/test_oauth.py` — PKCE derivation, the consent-URL parameters that
  decide whether a refresh token comes back, and code exchange against a stub.
- `tests/test_canvas.py` — Link-header pagination, UIC's underscored course
  codes, the UTC→campus-time conversion, and the import loop's four outcomes
  (create, update, adopt, refuse) against recorders.
- `tests/test_notion.py` — the property extraction the whole idempotency story
  rests on. Without it, reading `Canvas ID` back wrongly would duplicate every
  row on every run with a green suite.
- `tests/test_webauth.py`, `tests/test_crypto.py` — sessions, CSRF and
  encryption. Every test is named for the attack it prevents.
- `tests/test_store.py`, `tests/test_api_accounts.py` — the accounts half,
  against a real Postgres and real sockets: that a token is unreadable in the
  table, that an uninvited address cannot sign in, that a member cannot invite,
  that a session beats a stale bearer token.

The storage and account tests need a database:

```bash
docker run -e POSTGRES_PASSWORD=x -p 5432:5432 -d postgres:16
export TEST_DATABASE_URL=postgresql://postgres:x@localhost:5432/postgres
pytest -q
```

Without `TEST_DATABASE_URL` they skip rather than fail, so `pytest` stays a
one-command thing for anyone working on the matching logic. CI runs both ways.

Nothing here touches the network. The only external dependency is a local
Postgres for the storage tests, which is why they are opt-in.

## Limits

- Matching is on **filename only**. The PDF contents are never read or
  downloaded.
- GoodNotes' backup is one-way and runs on its own schedule, so a brand new
  notebook may take a few minutes to appear in Drive.
- Renaming a notebook creates a *new* PDF in Drive; the old one stays and shows
  up as an orphan until you delete it.
- An assignment deleted in Canvas is **not** deleted in Notion. Nothing here
  removes a row you might have written notes on.
- Only assignments are read from Canvas. Announcements, files, grades and
  submissions are not.

## Licence

MIT
