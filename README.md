# DokuTipp

DokuTipp helps you discover current documentaries from German public-media
libraries without searching each Mediathek yourself.

You tell *DokuTipp* which topics interest you. Then it finds suitable
programmes in the public [MediathekView](https://mediathekview.de/) film list,
and your preferred AI agent chooses four of them:

- three recommendations based on your interests;
- one extra recommendation outside your usual interests, to help you discover
  something new.

The result is a readable list with titles, descriptions, dates, durations,
broadcasters, and links to the programmes. Recommendations are remembered for
seven days so that the same programme is not immediately suggested again.

## Who is it for?

DokuTipp is useful if you like documentaries, reports, and in-depth programmes
but do not want to browse several Mediatheken manually.

It is a command-line tool, but you normally do not need to operate its commands
yourself. After installation and a one-time setup, your preferred AI agent uses
DokuTipp for you.

## Installation

DokuTipp requires macOS or Linux, Python 3.9 or newer, and `curl`.

The easiest way to install it is with
[pipx](https://pipx.pypa.io/stable/installation/). pipx keeps DokuTipp separate
from your other Python software and makes the `dokutipp` command available in
every terminal.

```bash
pipx install git+https://github.com/arturites/DokuTipp.git
```

Check the installation:

```bash
dokutipp
```

The first run starts the setup automatically.

<details>
<summary>Alternative: install a downloaded checkout</summary>

Use this option if you want to inspect or modify DokuTipp itself:

```bash
git clone https://github.com/arturites/DokuTipp.git
cd DokuTipp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools
python -m pip install -e .
dokutipp
```

After opening a new terminal, return to the checkout and activate the
environment again:

```bash
cd /path/to/DokuTipp
source .venv/bin/activate
```

Run `deactivate` when you want to leave the environment.

</details>

## First-time setup

DokuTipp asks you for:

1. documentary topics that interest you, entered comma-separated on one line,
   for example `history, science, nature`;
2. where your preferred AI agent loads its skills from;
3. optional topics you do not want recommended;
4. which broadcasters DokuTipp should include.

The broadcaster list uses arrow keys for navigation. Press space to include or
exclude the highlighted broadcaster and Enter to confirm. Included broadcasters
are shown as `- [x] ARD`; excluded broadcasters are shown as `- [ ] ARD`.
DokuTipp refreshes or reuses its MediathekView cache before showing this current
broadcaster list.

You can accept the suggested skill location or enter another one. If you choose
a different location, your AI agent must already know that directory as a
skill directory.

DokuTipp saves an editable `PROFILE.md` in the installed skill folder. You can
change your interests and excluded topics in that file at any time. Run
`dokutipp setup` if you want to repeat the setup or choose a different skill
location. Personal broadcaster exclusions are stored one literal name per line
in `~/.dokutipp/senders.txt`; blank lines and `#` comments are ignored. The
absolute path is stored as `sender_filter_file` in
`~/.dokutipp/config.json`, so it can be changed manually when needed.

## Getting recommendations

After setup, ask your agent in natural language, for example:

> Use DokuTipp to recommend some current documentaries.

The agent reads your profile, asks DokuTipp for current candidates page by
page, and returns the finished recommendations. DokuTipp automatically
downloads the current film list when needed and takes care of filtering, stable
pagination, links, formatting, and recently recommended programmes. The
personal broadcaster exclusions apply automatically to both candidate fetching
and final selection.

## Updating and troubleshooting

Update a pipx installation with:

```bash
pipx upgrade dokutipp
```

If your terminal cannot find the command, check whether pipx installed it and
whether it is on your `PATH`:

```bash
pipx list
command -v dokutipp
```

Run `pipx ensurepath` if pipx reports that its application directory is not on
your `PATH`, then open a new terminal.

Use `dokutipp --help` to see the available commands and filters.

<details>
<summary>CLI reference for agent integrations</summary>

DokuTipp deliberately separates editorial selection from reliable output:

1. `dokutipp fetch --limit 50 --page 1 [filters]` returns one deterministic
   candidate page as JSON.
2. The agent keeps suitable candidates and requests further pages with the
   same browsing parameters until it has a good selection. Candidates may
   come from different pages.
3. `dokutipp select "ID1,ID2,ID3,xID4" --limit 50 --page M [same filters]`
   validates the choice against all pages through `M` and returns the complete
   recommendation list as Markdown.

The lowercase `x` marks the extra recommendation. The agent must use complete
IDs from the fetched pages and repeat the browsing parameters with the latest
page reached for `select`. The fetch response includes `pagination.page`,
`pagination.total_pages`, `pagination.total_candidates`, and the inclusive
`candidate_range`. The page is calculated after filtering, deduplication, and
history exclusion; DokuTipp stores no pagination state between invocations.
DokuTipp owns the final text and formatting; the agent forwards that output
unchanged.

Both commands support these filters:

| Option | Default | Purpose |
| --- | --- | --- |
| `--limit N` | `50` | Number of candidates shown per page. |
| `--page N` | `1` | One-based candidate page to fetch or select. |
| `--min-duration MINUTES` | `42` | Exclude shorter programmes. |
| `--filter-file PATH` | bundled `filters.txt` | Use another title-exclusion list. |

The bundled `filters.txt` contains case-insensitive regular expressions, one
per line. Blank lines and lines beginning with `#` are ignored.
Broadcaster exclusions are personal configuration and therefore have no CLI
override. They are applied automatically while building the candidate pool.

</details>

## About the data source

DokuTipp is an independent project. It is not affiliated with, endorsed by, or
working with MediathekView. It only uses MediathekView's publicly accessible
film-list data.

## License

MIT
