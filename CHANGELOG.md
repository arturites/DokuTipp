# Changelog

All notable changes to DokuTipp are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - Unreleased

### Added

- Add a transparent, versioned `filters.txt` title-exclusion list with
  case-insensitive regular-expression support and a `--filter-file` override.
- Include the active title-exclusion patterns in the `fetch` filter metadata.

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

[2.0.0]: https://github.com/arturites/DokuTipp/compare/v1.3.0...HEAD
