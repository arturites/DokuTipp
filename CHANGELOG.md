# Changelog

All notable changes to DokuTipp are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Create the per-user `~/.dokutipp/` directory after onboarding, including
  `config.json` and the `data/` directory for the MediathekView cache and
  recommendation history.

### Changed

- Rework the README for first-time users by leading with DokuTipp's purpose and
  everyday agent workflow, moving low-level CLI details into a collapsible
  integration reference, and using agent-neutral wording.
- Use `~/.dokutipp/config.json` and `~/.dokutipp/data/` as the default paths,
  independent of the current working directory and `XDG_CONFIG_HOME`.
- Leave existing XDG, checkout, and working-directory state untouched instead
  of migrating it automatically; affected installations run onboarding again.
- Keep the existing `filters.txt` path resolution and packaging unchanged.

## [2.0.0] - 2026-08-15

### Added

- Add first-run CLI onboarding for interests, optional topics to avoid, and a
  Hermes or manually chosen skill root.
- Add `dokutipp setup` for explicitly changing the configured skill root.
- Install the bundled `SKILL.md` and a sibling editable `PROFILE.md` in a
  `dokutipp` directory beneath the configured skill root, and verify them on
  every CLI run.
- Add a transparent, versioned `filters.txt` title-exclusion list with
  case-insensitive regular-expression support and a `--filter-file` override.
- Include the active title-exclusion patterns in the `fetch` filter metadata.
- Add a local seven-day recommendation history at
  `data/recommendation-history.json`. Successful `select` runs store all four
  selected SHA-256 hashes, including the extra recommendation, with timestamps
  only; `fetch` omits exact matches for 7 x 24 hours and removes expired entries
  automatically.

### Changed

- Do not prefer specific broadcasters by default; include entries from all
  channels unless `--channels` is provided.
- Move the hard-coded `Audiodeskription` title exclusion into `filters.txt`,
  where additional exclusions such as `Mittagsmagazin` can be maintained.
- Make the installed `dokutipp` command, with its `fetch` and `select`
  subcommands, the only supported DokuTipp CLI entry point.
- Document the package CLI exclusively in the README and agent integration.
- Let the agent choose `--limit` and `--min-duration` values at its own
  discretion instead of treating the defaults as requirements.
- Do not impose a fixed default for `--limit`; `fetch` and `select` now use no
  candidate limit unless one is explicitly provided.
- Define the extra recommendation as a worthwhile documentary outside the
  interests in `PROFILE.md`, intended to broaden the user's horizons while
  continuing to respect topics to avoid.
- Require users of the removed source-checkout wrappers to migrate to
  `dokutipp fetch` and `dokutipp select`.

### Fixed

- Treat repeated MediathekView rows for the same logical candidate as one
  selectable ID while continuing to reject true SHA-256 collisions.

### Removed

- Remove the `scripts/start_curation.py` compatibility wrapper.
- Remove the `scripts/parse_filmliste.py` compatibility wrapper.

[2.0.0]: https://github.com/arturites/DokuTipp/compare/v1.3.0...v2.0.0
