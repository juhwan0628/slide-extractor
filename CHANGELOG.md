# Changelog

## 2026-10-05

### Changed
- Default detector changed to local-change detection.
- Default deduplication changed to `off` so detected temporal states are preserved.
- Added tile-level change metrics for small slide builds.
- Added temporal persistence and transient-return handling.
- Added stable representative-frame selection.
- Added schema v2 timeline metadata and PDF-page mapping.
- Added optional diagnostic JSON output.
- Added explicit legacy detector and global pHash dedup compatibility modes.

### Fixed
- Prevented non-EOF unstable candidates from being accepted as confirmed transitions.
- Reduced compression / decode-noise false positives by requiring changed-pixel evidence.
- Avoided removing non-adjacent repeated slide states from the default PDF.

### Validation
- 65 automated tests passing.
- Re-validated five real lecture videos with the new defaults.
- Broader cross-course / cross-layout benchmarking remains to be done.
