# DokuTipp

DokuTipp is a standalone command-line tool that finds current documentaries in
the public [MediathekView](https://mediathekview.de/) film list. An AI Agent chooses
only candidate IDs; DokuTipp validates that selection and renders the complete,
readable result itself. People and agents can use the final output directly.

## Why I started it

I started DokuTipp because I kept spending too much time searching several
Mediatheken for thoughtful documentaries, reports, and deep dives. I wanted a
small, reproducible way to surface recent candidates first and make the actual
choice easier.

## MediathekView disclaimer

DokuTipp is an independent project. It is not affiliated with, endorsed by, or
working with MediathekView; it only uses MediathekView's publicly accessible
film-list data.

## Installation

DokuTipp requires Python 3.9 or newer and `curl` on `PATH`.

### Recommended: install the checkout with pipx

Use pipx if `dokutipp` should be available from every terminal without manual
environment activation:

```bash
pipx install https://github.com/arturites/DokuTipp.git
```

If you want to update to a newer version use:

```bash
pipx upgrade dokutipp
```

### Alternative: install from a checkout in a virtual environment

The following steps work on macOS and Linux and keep DokuTipp's Python
dependencies separate from the rest of the system:

```bash
git clone https://github.com/arturites/DokuTipp.git
cd DokuTipp

python3 --version
curl --version

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -e .

dokutipp
```

`-e` installs the checkout in editable mode, so changes to the source are used
immediately. The first `dokutipp` invocation starts the interactive setup
described below. After opening a new terminal, activate the environment again
before using the command:

```bash
cd /path/to/DokuTipp
source .venv/bin/activate
dokutipp --help
```

Use `deactivate` to leave the virtual environment.

## First start and skill setup

On the first interactive `dokutipp` invocation, DokuTipp asks for your
interests, the agent setup to use, and optional topics to avoid. It then saves
the setup at `~/.dokutipp/config.json` and creates the empty data directory at
`~/.dokutipp/data/`.

The new per-user state is independent of the current working directory:

```text
~/.dokutipp/
├── config.json
└── data/
    ├── Filmliste-akt.xz
    └── recommendation-history.json
```

Existing installations are intentionally not migrated. The former
`$XDG_CONFIG_HOME/dokutipp/config.json` (or `~/.config/dokutipp/config.json`)
and old checkout or working-directory `data/` folders remain untouched. The
first command after this change runs onboarding again and starts with a fresh
cache and recommendation history.

At present, Hermes Agent is the supported agent choice. Its skill root is
`${HERMES_HOME:-~/.hermes}/skills`, so DokuTipp creates:

```text
${HERMES_HOME:-~/.hermes}/skills/dokutipp/
├── SKILL.md
└── PROFILE.md
```

You can instead enter a skill root manually. For a new root, DokuTipp creates
the `dokutipp` folder below it, copies its original `SKILL.md` there, and
creates the sibling `PROFILE.md` with the answers from onboarding. At an
existing root it checks those files and asks before replacing an existing
profile or a modified skill.

A manually chosen root must already be scanned by Hermes. DokuTipp does not
modify Hermes configuration or register external skill directories.

`PROFILE.md` is your editable personal context: change interests or topics to
avoid directly in that file whenever they change. The CLI does not overwrite an
existing profile during its normal checks. Run `dokutipp setup` to choose a new
skill root; it asks before replacing an existing profile.

Before every normal command, including `fetch`, `select`, and help, DokuTipp
checks that both files are present. `dokutipp setup` performs that setup itself
so it can deliberately reconfigure an existing installation. The preflight
restores a missing `SKILL.md` from the bundled original and asks to recreate a
missing profile. If the installed `SKILL.md` differs from the bundled original,
it asks whether it should restore the original. This leaves a declined local
edit intact. A command that needs setup or an answer cannot run
non-interactively: it exits with status 2 without writing a command result to
stdout.

## Fetch and select

DokuTipp has an explicit two-step workflow:

```
MediathekView data -> DokuTipp filtering -> LLM ID selection
-> DokuTipp validation and rendering -> final output on stdout
```

1. `dokutipp fetch [filters]` writes a machine-readable candidate set with
   stable IDs to stdout.
2. The AI Agent uses that candidate set and any profile context to select IDs only:
   three normal recommendations and one extra recommendation.
3. `dokutipp select IDS [same filters]` validates the selection, resolves the
   IDs to the original MediathekView records, and writes the final DokuTipp
   Markdown to stdout.

DokuTipp does not configure or invoke an LLM. The LLM has no responsibility for
headings, numbering, metadata, descriptions, links, labels, or any other
presentation logic.

### Fetch candidates

```bash
dokutipp fetch
```

`fetch` returns JSON with the filtered candidates, their IDs, the active
filters, and a `status` field. Select candidates only when `status` is
`"ready"`. `"no_candidates"` and `"insufficient_candidates"` mean that no
3+1 selection is possible; their `message` field is already generated by
DokuTipp. The active title patterns are listed in the
`filters.title_exclusions` field.

Each candidate contains its ID, title, broadcaster, date, duration, and source
description. The Mediathek URL is deliberately not part of the `fetch` payload;
DokuTipp resolves it from the original data during `select`. IDs are full
SHA-256 hashes of the candidate's title, duration, broadcaster, date, and URL.

After each successful `select`, DokuTipp stores all four selected hashes,
including the extra recommendation, with their timestamps in the local
`~/.dokutipp/data/recommendation-history.json`. Subsequent `fetch` calls omit exact hash
matches for seven days (7 x 24 hours). Expired entries are removed on the next
history access, so those candidates can be recommended again without a manual
reset. A damaged local history is reset automatically with a warning on stderr.
The history contains only hashes and timestamps. If any identity field used by
the hash changes, the source record receives a different ID and is not
suppressed by the earlier history entry.

### Submit a selection

Pass exactly four complete candidate IDs from the preceding `fetch` result as
one comma-separated argument. Prefix exactly one ID with a lowercase `x` to
mark the extra recommendation. The three unprefixed IDs retain their supplied
order in the normal recommendations.

```bash
dokutipp select "ID1,ID2,ID3,xID4"
```

Candidate IDs are full lowercase SHA-256 hashes. They are valid only for the
matching candidate set. Repeat the exact same filter arguments from `fetch`
when running `select`, including `--filter-file` when you use a custom list:

```bash
dokutipp fetch --limit 50 --min-duration 60 --channels ARD ZDF \
  --filter-file filters.txt
dokutipp select "ID1,ID2,ID3,xID4" --limit 50 --min-duration 60 \
  --channels ARD ZDF --filter-file filters.txt
```

The `select` stdout is the complete final DokuTipp Markdown. Progress and error
messages go to stderr. After setup, `dokutipp` without a subcommand prints help
to stderr and exits with status 2; the first bare invocation performs setup and
exits successfully.

### Filters

The following options are available on both `fetch` and `select`:

| Option | Default | Description |
| --- | --- | --- |
| `--limit N` | no limit | Maximum number of filtered source candidates. |
| `--min-duration MINUTES` | `42` | Exclude entries shorter than this duration. |
| `--channels CHANNEL [CHANNEL ...]` | all channels | Broadcasters to include. |
| `--filter-file PATH` | `filters.txt` | File with title-exclusion regular expressions. |

The default `filters.txt` is stored in the repository root and is bundled for
installed CLI builds. It contains one case-insensitive regular expression per
line. Empty lines and lines beginning with `#` are ignored; patterns are
matched only against programme titles. Add a new title pattern to this file,
or pass another file with `--filter-file` to both `fetch` and `select`.

The default filter uses a 24-hour cache at
`~/.dokutipp/data/Filmliste-akt.xz`, downloads the list when necessary,
considers entries from all broadcasters from the past seven days, and excludes
future entries and titles matching the patterns listed in `filters.txt`. The
`filters.txt` lookup and packaging remain unchanged. Pass `--channels` to
restrict the broadcaster selection.

## Skill integration

The installed `dokutipp/SKILL.md` is a thin agent integration layer. It tells
the agent to read its sibling `PROFILE.md`, make the Fetch/Select ID choice,
and forward the final `select` stdout unchanged. DokuTipp owns all final
presentation; agents must not add their own formatting.

## License

MIT
