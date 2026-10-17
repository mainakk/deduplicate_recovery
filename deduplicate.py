#!/usr/bin/env python3
"""Copy one deterministic representative of each recovered PDF content hash."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TextIO

CONFIG_NAME = "config.json"
DEFAULT_REPORT_NAME = "deduplication_report.json"
DEFAULT_CACHE_NAME = "pdf_hash_cache.sqlite3"
PROGRESS_INTERVAL = 100
CHUNK_SIZE = 1024 * 1024
MAX_PDF_SIZE_BYTES = 500_000_000


class ConfigurationError(ValueError):
    """Raised when the cwd configuration is missing or unsafe."""


@dataclass(frozen=True)
class Config:
    source_dir: Path
    destination_dir: Path
    report_path: Path
    dry_run_report_path: Path
    hash_cache_path: Path


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def load_config(
    cwd: Optional[Path] = None, *, allow_existing_destination: bool = False
) -> Config:
    cwd = (cwd or Path.cwd()).resolve()
    config_path = cwd / CONFIG_NAME
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"Cannot read {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"Invalid JSON in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigurationError(f"{config_path} must contain a JSON object")
    allowed = {
        "source_dir",
        "destination_dir",
        "report_path",
        "dry_run_report_path",
        "hash_cache_path",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ConfigurationError(
            f"Unknown config key(s): {', '.join(sorted(unknown))}"
        )

    def configured_path(key: str, default: Optional[Path] = None) -> Path:
        value = data.get(key)
        if value is None and default is not None:
            return default.resolve()
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(f"{key} must be a non-empty path string")
        path = Path(value).expanduser()
        return (cwd / path).resolve() if not path.is_absolute() else path.resolve()

    source_dir = configured_path("source_dir")
    destination_dir = configured_path("destination_dir")
    report_path = configured_path("report_path", cwd / DEFAULT_REPORT_NAME)
    dry_run_report_path = configured_path(
        "dry_run_report_path", cwd / "deduplication_dry_run_report.json"
    )
    hash_cache_path = configured_path("hash_cache_path", cwd / DEFAULT_CACHE_NAME)
    config_resolved = config_path.resolve()

    if not source_dir.is_dir():
        raise ConfigurationError(
            f"source_dir is not an existing directory: {source_dir}"
        )
    if (
        source_dir == destination_dir
        or _is_within(source_dir, destination_dir)
        or _is_within(destination_dir, source_dir)
    ):
        raise ConfigurationError("source_dir and destination_dir must not overlap")
    if _is_within(report_path, source_dir):
        raise ConfigurationError("report_path must be outside source_dir")
    if _is_within(report_path, destination_dir) or _is_within(
        destination_dir, report_path
    ):
        raise ConfigurationError("report_path and destination_dir must not overlap")
    if report_path == config_resolved:
        raise ConfigurationError("report_path must not overwrite config.json")
    if report_path.exists() and report_path.is_dir():
        raise ConfigurationError(f"report_path is a directory: {report_path}")
    if _is_within(dry_run_report_path, source_dir):
        raise ConfigurationError("dry_run_report_path must be outside source_dir")
    if _is_within(dry_run_report_path, destination_dir) or _is_within(
        destination_dir, dry_run_report_path
    ):
        raise ConfigurationError(
            "dry_run_report_path and destination_dir must not overlap"
        )
    if (
        dry_run_report_path == report_path
        or _is_within(dry_run_report_path, report_path)
        or _is_within(report_path, dry_run_report_path)
    ):
        raise ConfigurationError(
            "dry_run_report_path and report_path must not overlap"
        )
    if dry_run_report_path == config_resolved:
        raise ConfigurationError("dry_run_report_path must not overwrite config.json")
    if dry_run_report_path.exists() and dry_run_report_path.is_dir():
        raise ConfigurationError(
            f"dry_run_report_path is a directory: {dry_run_report_path}"
        )
    if _is_within(hash_cache_path, source_dir):
        raise ConfigurationError("hash_cache_path must be outside source_dir")
    if _is_within(hash_cache_path, destination_dir) or _is_within(
        destination_dir, hash_cache_path
    ):
        raise ConfigurationError("hash_cache_path and destination_dir must not overlap")
    if _is_within(hash_cache_path, report_path) or _is_within(
        report_path, hash_cache_path
    ):
        raise ConfigurationError("hash_cache_path and report_path must not overlap")
    if _is_within(hash_cache_path, dry_run_report_path) or _is_within(
        dry_run_report_path, hash_cache_path
    ):
        raise ConfigurationError(
            "hash_cache_path and dry_run_report_path must not overlap"
        )
    if hash_cache_path == config_resolved:
        raise ConfigurationError("hash_cache_path must not overwrite config.json")
    if hash_cache_path.exists() and hash_cache_path.is_dir():
        raise ConfigurationError(f"hash_cache_path is a directory: {hash_cache_path}")
    if os.path.lexists(destination_dir) and not allow_existing_destination:
        raise ConfigurationError(
            f"destination_dir already exists; choose a new, empty path: {destination_dir}"
        )
    return Config(
        source_dir,
        destination_dir,
        report_path,
        dry_run_report_path,
        hash_cache_path,
    )


def discover_pdfs(source_dir: Path) -> list[Path]:
    """Return regular PDFs in sorted relative-path order, rejecting symlinks."""
    paths: list[Path] = []
    for root, dirs, files in os.walk(source_dir, followlinks=False):
        root_path = Path(root)
        linked_dirs = [name for name in dirs if (root_path / name).is_symlink()]
        if linked_dirs:
            raise OSError(
                f"Symbolic-link directory in source tree: {root_path / linked_dirs[0]}"
            )
        dirs.sort()
        for name in files:
            path = root_path / name
            if path.is_symlink():
                if path.suffix.lower() == ".pdf":
                    raise OSError(f"Symbolic-link PDF in source tree: {path}")
                continue
            if path.suffix.lower() == ".pdf" and path.is_file():
                paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(source_dir).as_posix())


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


class HashCache:
    """Cache file hashes while invalidating entries when file metadata changes."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                filepath TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            )
            """
        )

    def __enter__(self) -> HashCache:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def get_or_compute(self, path: Path) -> tuple[str, int]:
        resolved_path = path.resolve()
        metadata = resolved_path.stat()
        fingerprint = (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_dev,
            metadata.st_ino,
        )
        row = self.connection.execute(
            """
            SELECT size, mtime_ns, ctime_ns, device, inode, sha256
            FROM file_hashes WHERE filepath = ?
            """,
            (str(resolved_path),),
        ).fetchone()
        if row is not None and tuple(row[:5]) == fingerprint:
            return row[5], metadata.st_size

        digest = hash_file(resolved_path)
        final_metadata = resolved_path.stat()
        final_fingerprint = (
            final_metadata.st_size,
            final_metadata.st_mtime_ns,
            final_metadata.st_ctime_ns,
            final_metadata.st_dev,
            final_metadata.st_ino,
        )
        if final_fingerprint != fingerprint:
            raise OSError(f"File changed while hashing: {resolved_path}")
        self.connection.execute(
            """
            INSERT INTO file_hashes
                (filepath, size, mtime_ns, ctime_ns, device, inode, sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                ctime_ns = excluded.ctime_ns,
                device = excluded.device,
                inode = excluded.inode,
                sha256 = excluded.sha256
            """,
            (str(resolved_path), *fingerprint, digest),
        )
        return digest, metadata.st_size


def output_name_for(relative_path: str) -> str:
    return relative_path.replace("/", "_")


def _copy_and_hash(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        while chunk := input_stream.read(CHUNK_SIZE):
            output_stream.write(chunk)
            digest.update(chunk)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    return digest.hexdigest()


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(report, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def run_deduplication(
    config: Config,
    progress_stream: TextIO = sys.stdout,
    *,
    dry_run: bool = False,
) -> dict:
    pdfs = discover_pdfs(config.source_dir)
    if dry_run:
        print(f"Dry run: scanning {len(pdfs)} PDFs...", file=progress_stream, flush=True)
    groups: dict[str, list[str]] = {}
    sizes: dict[str, int] = {}
    outliers: list[dict] = []
    eligible_processed = 0
    last_progress_count = 0
    with HashCache(config.hash_cache_path) as cache:
        for index, path in enumerate(pdfs, start=1):
            relative = path.relative_to(config.source_dir).as_posix()
            size = path.stat().st_size
            if size > MAX_PDF_SIZE_BYTES:
                outliers.append({"path": relative, "size_bytes": size})
            else:
                digest, size = cache.get_or_compute(path)
                groups.setdefault(digest, []).append(relative)
                sizes[relative] = size
                eligible_processed += 1
            if index % PROGRESS_INTERVAL == 0:
                duplicates = eligible_processed - len(groups)
                print(
                    f"Processed {index}/{len(pdfs)} PDFs; deduplicated {duplicates}; "
                    f"remaining {len(pdfs) - index}.",
                    file=progress_stream,
                    flush=True,
                )
                last_progress_count = index

    if dry_run and len(pdfs) != last_progress_count:
        duplicates = eligible_processed - len(groups)
        print(
            f"Processed {len(pdfs)}/{len(pdfs)} PDFs; deduplicated {duplicates}; "
            "remaining 0.",
            file=progress_stream,
            flush=True,
        )

    name_to_sources: dict[str, list[str]] = {}
    for relatives in groups.values():
        representative = relatives[0]
        output_name = output_name_for(representative)
        name_to_sources.setdefault(output_name.casefold(), []).append(representative)
    collisions = [
        {"copied_to": output_name_for(representatives[0]), "source_paths": representatives}
        for representatives in name_to_sources.values()
        if len(representatives) > 1
    ]

    report_groups: list[dict] = []
    for digest, relatives in groups.items():
        representative = relatives[0]
        output_name = output_name_for(representative)
        has_collision = len(name_to_sources[output_name.casefold()]) > 1
        report_groups.append(
            {
                "sha256": digest,
                "copied_from": representative,
                "copied_to": None if has_collision else output_name,
                "filename_collision": has_collision,
                "duplicates": relatives[1:],
            }
        )

    print(
        f"Hashing complete: {eligible_processed} PDFs hashed; "
        f"{len(report_groups)} unique content groups identified; "
        f"{len(outliers)} oversized outliers skipped.",
        file=progress_stream,
        flush=True,
    )

    report = {
        "schema_version": 1,
        "algorithm": "sha256",
        "dry_run": dry_run,
        "source_dir": str(config.source_dir),
        "destination_dir": str(config.destination_dir),
        "source_pdf_count": len(pdfs),
        "source_total_bytes": sum(sizes.values()) + sum(
            item["size_bytes"] for item in outliers
        ),
        "eligible_pdf_count": len(pdfs) - len(outliers),
        "eligible_total_bytes": sum(sizes.values()),
        "outlier_count": len(outliers),
        "outlier_total_bytes": sum(item["size_bytes"] for item in outliers),
        "outliers": outliers,
        "unique_count": len(report_groups),
        "duplicate_count": len(sizes) - len(report_groups),
        "unique_total_bytes": sum(sizes[group["copied_from"]] for group in report_groups),
        "copied_pdf_count": sum(not group["filename_collision"] for group in report_groups),
        "copied_total_bytes": sum(
            sizes[group["copied_from"]]
            for group in report_groups
            if not group["filename_collision"]
        ),
        "collision_count": len(collisions),
        "collisions": collisions,
        "groups": report_groups,
    }

    if dry_run:
        _print_outliers(outliers, progress_stream)
        _print_size_summary(report, progress_stream)
        _write_report(config.dry_run_report_path, report)
        print("Dry run: no files copied.", file=progress_stream)
        print(
            f"Dry-run report written to {config.dry_run_report_path}.",
            file=progress_stream,
        )
        _print_collisions(collisions, progress_stream)
        return report

    config.destination_dir.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(config.destination_dir):
        raise ConfigurationError(
            f"destination_dir appeared during processing: {config.destination_dir}"
        )

    stage_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{config.destination_dir.name}.staging-",
            dir=config.destination_dir.parent,
        )
    )
    committed_destination = False
    try:
        for group in report_groups:
            if group["filename_collision"]:
                continue
            source = config.source_dir / group["copied_from"]
            target = stage_dir / group["copied_to"]
            copied_hash = _copy_and_hash(source, target)
            if copied_hash != group["sha256"]:
                raise OSError(
                    f"Source changed while copying {group['copied_from']}; "
                    "the destination was not committed"
                )

        if os.path.lexists(config.destination_dir):
            raise ConfigurationError(
                f"destination_dir appeared during processing: {config.destination_dir}"
            )
        os.replace(stage_dir, config.destination_dir)
        committed_destination = True

        _write_report(config.report_path, report)
    except Exception:
        if committed_destination:
            shutil.rmtree(config.destination_dir)
        raise
    finally:
        if stage_dir.exists():
            shutil.rmtree(stage_dir)

    print(
        f"Processed {len(pdfs)}/{len(pdfs)} PDFs; deduplicated "
        f"{eligible_processed - len(report_groups)}; remaining 0.",
        file=progress_stream,
    )
    print(
        f"Copied {report['copied_pdf_count']} unique PDFs to {config.destination_dir}.",
        file=progress_stream,
    )
    _print_size_summary(report, progress_stream)
    print(f"Report written to {config.report_path}.", file=progress_stream)
    _print_outliers(outliers, progress_stream)
    _print_collisions(collisions, progress_stream)
    return report


def _print_collisions(collisions: list[dict], stream: TextIO) -> None:
    if not collisions:
        print("Filename collisions: none.", file=stream)
        return
    print(f"Filename collisions requiring manual review: {len(collisions)}", file=stream)
    for collision in collisions:
        print(
            f"  {collision['copied_to']}: {', '.join(collision['source_paths'])}",
            file=stream,
        )


def _print_outliers(outliers: list[dict], stream: TextIO) -> None:
    if not outliers:
        print("Oversized PDF outliers: none.", file=stream)
        return
    total_bytes = sum(item["size_bytes"] for item in outliers)
    print(
        f"Skipped oversized PDF outliers (>500 MB): {len(outliers)} "
        f"({format_size(total_bytes)} total).",
        file=stream,
    )
    for outlier in outliers:
        print(
            f"  {outlier['path']}: {format_size(outlier['size_bytes'])}",
            file=stream,
        )


def _print_size_summary(report: dict, stream: TextIO) -> None:
    print(
        f"Source: {report['source_pdf_count']} PDFs, "
        f"{format_size(report['source_total_bytes'])}.",
        file=stream,
    )
    print(
        f"Eligible source: {report['eligible_pdf_count']} PDFs, "
        f"{format_size(report['eligible_total_bytes'])}.",
        file=stream,
    )
    print(
        f"Unique: {report['unique_count']} PDFs, "
        f"{format_size(report['unique_total_bytes'])} (would save "
        f"{format_size(report['eligible_total_bytes'] - report['unique_total_bytes'])}).",
        file=stream,
    )
    print(
        f"Copyable: {report['copied_pdf_count']} PDFs, "
        f"{format_size(report['copied_total_bytes'])}.",
        file=stream,
    )


def format_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report source and unique PDF counts/sizes without copying files",
    )
    args = parser.parse_args()
    try:
        config = load_config(allow_existing_destination=args.dry_run)
        run_deduplication(config, dry_run=args.dry_run)
    except (ConfigurationError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Deduplication failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
