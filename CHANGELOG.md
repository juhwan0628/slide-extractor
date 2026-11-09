# Changelog

## 0.5.0b2 — shared-engine CLI prerelease candidate

- Replace the legacy FPS-based implementation with the same keyframe extraction core used by the GUI development repository.
- Add installed `slide-extractor` command, single-file and sequential batch processing, optional simple/detailed JSON, cancellation and protected output publishing.
- Keep GUI dependencies out of this CLI distribution.
- Record the development source commit and per-file hashes in source-manifest.json.

The legacy 0.1.0 source remains in this repository's Git history and preservation branch. See the README migration table for incompatible options, output defaults and JSON schemas.
