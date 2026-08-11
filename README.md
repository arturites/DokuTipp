# DokuTipp

DokuTipp is a standalone command-line tool that downloads the public
[MediathekView](https://mediathekview.de/) film list and emits filtered
documentary candidates as JSON. It does not rank recommendations, use an LLM,
or deliver messages; those concerns remain outside the CLI.

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

### Recommended: install from a checkout in a virtual environment

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

dokutipp --help
```

`-e` installs the checkout in editable mode, so changes to the source are used
immediately. To run the tool:

```bash
dokutipp
```

After opening a new terminal, activate the environment again before using the
command:

```bash
cd /path/to/DokuTipp
source .venv/bin/activate
dokutipp
```

Use `deactivate` to leave the virtual environment.

### Alternative: install the checkout with pipx

Use pipx if `dokutipp` should be available from every terminal without manual
environment activation:

```bash
git clone https://github.com/arturites/DokuTipp.git
cd DokuTipp

python3 -m pip install --user pipx
python3 -m pipx ensurepath
# Open a new terminal so the PATH change takes effect.

pipx install .
dokutipp --help
```

### If `dokutipp` is not found

For the virtual-environment installation, make sure that
`source .venv/bin/activate` has run in the current terminal. For pipx, open a
new terminal after `python3 -m pipx ensurepath`, then check the command with:

```bash
command -v dokutipp
```

## Usage

```bash
dokutipp
```

Show the available options:

```bash
dokutipp --help
```

The standard command reproduces the former `start_curation.py` workflow:

- keeps a 24-hour cache at `data/Filmliste-akt.xz` (the repository cache in a
  source checkout, otherwise the current directory);
- downloads the official list when that cache is missing or stale;
- includes ARD, ZDF, and ARTE.DE entries from the last seven days;
- excludes future entries and titles containing `Audiodeskription`;
- excludes entries shorter than 42 minutes; and
- outputs at most 1337 candidates as formatted JSON.

Progress messages retain the legacy launcher's stdout behavior; when stdout is
captured, buffered status messages can appear after the parser's JSON.

### Options

Only the existing filtering settings are exposed:

| Option | Default | Description |
| --- | --- | --- |
| `--limit N` | `1337` | Maximum number of output entries. |
| `--min-duration MINUTES` | `42` | Exclude entries shorter than this duration. |
| `--channels CHANNEL [CHANNEL ...]` | `ARD ZDF ARTE.DE` | Channels to include. |

For example:

```bash
dokutipp --limit 50 --min-duration 60 --channels ARD ZDF
```

## Legacy OpenClaw skill

DokuTipp is now primarily a CLI. Agents should invoke `dokutipp` directly.
The existing OpenClaw `SKILL.md` is retained as a deprecated compatibility
integration; it is no longer the primary integration path and may be removed
in a future major release.

For source checkouts that have not yet installed the package, the former entry
point remains available as a compatibility wrapper and runs the same defaults:

```bash
python3 scripts/start_curation.py
```

## License

MIT
