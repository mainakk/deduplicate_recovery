# Recovered PDF deduplication

The scripts use only Python's standard library. Copy `config.example.json` to
`config.json` in the working directory and set the recovered PDF directory and
a new destination directory. Paths may be absolute or relative to the working
directory. The optional `report_path` setting names the JSON report file; when
omitted, it defaults to `deduplication_report.json` in the working directory.
The optional `dry_run_report_path` setting names the dry-run JSON report; by
default it is `deduplication_dry_run_report.json` in the working directory.
The optional `hash_cache_path` setting selects the SQLite hash cache; by
default it is `pdf_hash_cache.sqlite3` in the working directory. Source,
destination, report, and cache paths must not overlap, and the destination must
not already exist for a normal run.

Run both scripts from the directory containing `config.json`:

```sh
python3 deduplicate.py
python3 deduplicate.py --dry-run
python3 verify_deduplication.py
```

The deduplicator accepts `--dry-run`; the verifier accepts no command-line
arguments. PDFs are discovered recursively, case-insensitively by `.pdf`
extension, and hashed with SHA-256. Hashes are cached by absolute filepath in
SQLite. The cache fingerprint includes file size, modification/change times,
device, and inode; files with changed metadata are rehashed. For identical
content, the lexicographically first source-relative path is copied. Output is
flat, with path separators in the source-relative path replaced by underscores;
output-name collisions are listed for manual review and those conflicting
representatives are omitted while other unique files continue to copy. The
deduplicator stages the output and commits it only after all copies match their
source hashes. It prints progress every 100 source PDFs and lists all
collisions at the end.

PDFs larger than 500 MB (500,000,000 bytes) are treated as outliers: they are
not hashed, deduplicated, cached, or copied. Both normal and dry-run modes list
each skipped path and size in the final console summary and record them in the
JSON report (`outliers`, `outlier_count`, and `outlier_total_bytes`). Source
totals include these files; eligible-source and unique totals exclude them.
Console size summaries use human-readable binary units (B, KiB, MiB, GiB, ...);
the JSON report retains exact byte counts.

Dry run looks up or computes hashes, then reports the source PDF count and total
bytes against the unique count and total bytes, including potential byte
savings. It writes a separate JSON report marked `"dry_run": true`, but does
not create copies or overwrite the normal verification report. It also updates
the hash cache so later runs can reuse computed hashes.
The configured destination may already exist during a dry run. Dry-run output
also lists every resulting filename collision and reports how many unique files
can be copied safely. The verifier rejects dry-run reports because they describe
planned output rather than copied files.
Normal runs print the same source/unique/copyable size summary after copying,
in addition to storing these totals in the JSON report.

Both modes print a flushed `Hashing complete` marker after all PDFs have been
hashed (or matched from cache) and collision groups determined. In normal mode
this appears before copying begins.

The verifier uses the same config and report to compare every source PDF with
the report and copied file, confirm hash groups and deterministic names, and
reject extra or missing destination files.

Run the tests with:

```sh
python3 -m unittest discover -s tests
```
