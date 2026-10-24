# Copilot instructions for deduplicate_recovery

## Repository purpose

This repository contains a Python tool for deduplicating recovered PDF files by SHA-256 hash while preserving the original source tree. It is designed for large recovered-directory inventories where many files are duplicates, and the script must never modify the source directory in place.

## Commands

Run from the directory containing `config.json`.

- Full suite:
  - `python3 -m unittest discover -s tests`
- Single test:
  - `python3 -m unittest tests.test_deduplication.DeduplicationTests.test_dry_run_lists_filename_collisions_and_counts_copyable_files`
- Normal deduplication run:
  - `python3 deduplicate.py`
- Dry run (inventory and collision reporting without copying):
  - `python3 deduplicate.py --dry-run`
- Verification against the produced report and copied output:
  - `python3 verify_deduplication.py`

## Configuration and execution model

The project expects a `config.json` file in the working directory. Copy from `config.example.json` and set:

- `source_dir`: recovered PDF tree to scan
- `destination_dir`: new deduplicated output directory
- `report_path`: normal JSON report path
- `dry_run_report_path`: dry-run report path
- `hash_cache_path`: SQLite cache for previously computed hashes

Key rules enforced by the code:

- Paths are resolved relative to the config file's directory unless absolute.
- `source_dir` and `destination_dir` must not overlap.
- `source_dir` must remain untouched; deduplication writes only to `destination_dir`.
- In normal mode, `destination_dir` must not already exist.
- The hash cache, report files, and config file must not overlap with each other or the source/destination directories.
- Symlinked files/directories inside the source tree are rejected.

## High-level architecture

### `deduplicate.py`

This is the main pipeline.

- `load_config()` validates the runtime configuration and path safety rules.
- `discover_pdfs()` recursively collects PDF files while rejecting symlinks and sorting by source-relative path.
- `HashCache` stores SHA-256 digests in SQLite and invalidates entries when a file's size, mtimes, ctime, device, or inode changes.
- `run_deduplication()` orchestrates the full flow:
  - scan the source tree
  - exclude files larger than 500 MB as outliers
  - hash eligible PDFs with the cache
  - group duplicates by content hash
  - choose the lexicographically first source-relative path as the representative
  - compute a deterministic output name with `relative_path.replace("/", "_")`
  - record collisions when multiple representatives would map to the same output filename
  - stage copies to a temporary directory and atomically rename into the destination directory only after successful validation
  - write the JSON report

### `verify_deduplication.py`

This script verifies that the report and copied output are consistent with the source tree.

- It reloads the JSON report and ensures it matches the config's `source_dir` and `destination_dir`.
- It recomputes hashes for all eligible PDFs from the source and checks every reported group.
- It rejects dry-run reports, mismatched source inventories, duplicate hash groups, missing output files, invalid filename collisions, and non-matching copied-file hashes.

### `tests/test_deduplication.py`

The tests exercise deterministic naming, duplicate-group behavior, filename collisions, dry-run output, outlier handling, and progress reporting. They create temporary directories and validate both the report and the copied files end-to-end.

## Key conventions specific to this codebase

- Python standard library only; no external dependencies are required.
- Deterministic dedupe order is source-relative path order, not filesystem order.
- Duplicate representatives are chosen by the smallest source-relative path string (`min(source_members)` in verification), which is why the project uses a stable sort on relative paths before grouping.
- Output filenames are flat in the destination directory; subdirectory separators are replaced with underscores, so `A/x.pdf` becomes `A_x.pdf`.
- File collisions are reported as manual-review items, and colliding representatives are omitted while other unique files still copy.
- Oversized PDFs (>500,000,000 bytes) are excluded from hashing, dedupe, and copy steps; they are recorded in `outliers`, `outlier_count`, and `outlier_total_bytes` in both the console summary and JSON report.
- The script prints periodic progress markers like `Processed 100/101 PDFs...` and `Hashing complete...`; this is part of the expected CLI behavior and should be preserved when editing.
- Report writing is atomic: writes happen to a temporary file in the same directory and then `os.replace()` is used to avoid partial writes.
- Copying is staged in a temporary sibling directory and only committed once all copies succeed and the destination still does not exist; this avoids leaving a partially copied destination behind.

## Working with the code

When making changes, prefer the existing patterns in `deduplicate.py` and `verify_deduplication.py` rather than introducing new frameworks or alternative outputs. The main correctness boundaries are:

- config validation and path safety
- deterministic group ordering
- stable output naming
- atomic staged copying
- report verification rules
