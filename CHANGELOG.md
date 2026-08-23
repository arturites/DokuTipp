# Changelog

All notable changes to DokuTipp are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add deterministic, stateless candidate pagination to `fetch` and `select`.
  `--limit` defaults to 50 and `--page` defaults to 1; fetch responses expose
  the page, total page count, total candidate count, and inclusive candidate
  range.

### Changed

- Build pages only after complete filtering, deduplication, and recommendation
  history exclusion, so changing the source order does not change page
  membership.
- Allow one final selection to combine candidates collected across all pages
  browsed through the supplied `--page` value.
- Keep fetch responses focused on status, pagination, candidates, and relevant
  error messages instead of exposing internal filter and selection metadata.
- Update the installed agent skill to evaluate each page and request the next
  page itself when the current candidates are not a sufficiently good match.

## [2.1.0] - 2026-08-21

### Added

- Create the per-user `~/.dokutipp/` directory after onboarding, including
  `config.json` and the `data/` directory for the MediathekView cache and
  recommendation history.
- Add an interactive broadcaster selection to onboarding with arrow-key and
  space-bar controls and plain `- [x]`/`- [ ]` checkbox markers. Store unchecked
  broadcasters as literal, case-insensitive exclusions in the personal
  `~/.dokutipp/senders.txt` file.
- Store the absolute personal sender-filter path as `sender_filter_file` in
  `~/.dokutipp/config.json` and include active exclusions in fetch metadata as
  `filters.excluded_channels`.

### Changed

- Rework the README for first-time users by leading with DokuTipp's purpose and
  everyday agent workflow, moving low-level CLI details into a collapsible
  integration reference, and using agent-neutral wording.
- Use `~/.dokutipp/config.json` and `~/.dokutipp/data/` as the default paths,
  independent of the current working directory and `XDG_CONFIG_HOME`.
- Leave existing XDG, checkout, and working-directory state untouched instead
  of migrating it automatically; affected installations run onboarding again.
- Keep the existing `filters.txt` path resolution and packaging unchanged.
- Refresh the MediathekView cache atomically during setup and fall back to a
  readable existing cache when a refresh fails.
- Apply the personal broadcaster exclusions automatically to both `fetch` and
  `select` instead of requiring repeated command-line values.

### Removed

- Remove the `--channels` broadcaster inclusion filter from the supported CLI
  and parser API.

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

[Unreleased]: https://github.com/arturites/DokuTipp/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/arturites/DokuTipp/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/arturites/DokuTipp/compare/v1.3.0...v2.0.0
