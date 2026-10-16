#!/usr/bin/env python3
"""Verify a recovered-PDF deduplication report and its copied files."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from deduplicate import (
    Config,
    ConfigurationError,
    discover_pdfs,
    hash_file,
    load_config,
    output_name_for,
)

HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(ValueError):
    """Raised when the report or output directory does not verify."""


def _load_report(path: Path) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise VerificationError(f"Cannot read report {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise VerificationError(f"Invalid JSON in report {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise VerificationError("Report must contain a JSON object")
    return report


def _destination_files(directory: Path) -> set[str]:
    found: set[str] = set()
    for root, dirs, files in os.walk(directory, followlinks=False):
        root_path = Path(root)
        if any((root_path / name).is_symlink() for name in dirs):
            raise VerificationError(f"Symbolic-link directory in destination: {root_path}")
        if dirs:
            raise VerificationError(f"Unexpected subdirectory in destination: {root_path}")
        for name in files:
            path = root_path / name
            if path.is_symlink():
                raise VerificationError(f"Symbolic link in destination: {path}")
            if path.is_file():
                found.add(path.relative_to(directory).as_posix())
    return found


def verify_report(config: Config) -> int:
    report = _load_report(config.report_path)
    if report.get("schema_version") != 1 or report.get("algorithm") != "sha256":
        raise VerificationError("Unsupported report schema or hash algorithm")
    if report.get("source_dir") != str(config.source_dir):
        raise VerificationError("Report source_dir does not match config.json")
    if report.get("destination_dir") != str(config.destination_dir):
        raise VerificationError("Report destination_dir does not match config.json")
    if not config.destination_dir.is_dir():
        raise VerificationError(f"Destination directory does not exist: {config.destination_dir}")

    source_paths = discover_pdfs(config.source_dir)
    source_by_relative = {
        path.relative_to(config.source_dir).as_posix(): hash_file(path)
        for path in source_paths
    }
    source_sizes = {
        path.relative_to(config.source_dir).as_posix(): path.stat().st_size
        for path in source_paths
    }
    groups = report.get("groups")
    if not isinstance(groups, list):
        raise VerificationError("Report groups must be a JSON array")

    reported_sources: set[str] = set()
    reported_destinations: set[str] = set()
    reported_hashes: set[str] = set()
    representatives_by_name: dict[str, list[str]] = {}
    group_collisions: dict[str, bool] = {}
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise VerificationError(f"Report group {index} must be a JSON object")
        digest = group.get("sha256")
        representative = group.get("copied_from")
        copied_to = group.get("copied_to")
        duplicates = group.get("duplicates")
        if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            raise VerificationError(f"Report group {index} has an invalid SHA-256")
        if digest in reported_hashes:
            raise VerificationError(f"Duplicate hash groups in report: {digest}")
        reported_hashes.add(digest)
        if not isinstance(representative, str) or not representative:
            raise VerificationError(f"Report group {index} has an invalid copied_from")
        if copied_to is not None and (
            not isinstance(copied_to, str)
            or not copied_to
            or Path(copied_to).name != copied_to
        ):
            raise VerificationError(f"Report group {index} has an unsafe copied_to")
        filename_collision = group.get("filename_collision")
        if not isinstance(filename_collision, bool):
            raise VerificationError(f"Report group {index} has an invalid collision flag")
        if not isinstance(duplicates, list) or any(
            not isinstance(item, str) or not item for item in duplicates
        ):
            raise VerificationError(f"Report group {index} has an invalid duplicates list")

        source_members = [representative, *duplicates]
        if source_members != sorted(source_members):
            raise VerificationError(
                f"Source paths in group {index} are not in deterministic order"
            )
        if len(source_members) != len(set(source_members)):
            raise VerificationError(f"Repeated source path in report group {index}")
        for relative in source_members:
            if relative in reported_sources:
                raise VerificationError(f"Source file appears in multiple groups: {relative}")
            reported_sources.add(relative)
            actual_hash = source_by_relative.get(relative)
            if actual_hash is None:
                raise VerificationError(f"Reported source PDF is missing: {relative}")
            if actual_hash != digest:
                raise VerificationError(f"Source PDF hash mismatch: {relative}")
        if not source_members or representative != min(source_members):
            raise VerificationError(f"Non-deterministic representative in group {index}")
        expected_name = output_name_for(representative)
        representatives_by_name.setdefault(expected_name.casefold(), []).append(
            representative
        )
        group_collisions[representative] = filename_collision
        if filename_collision:
            if copied_to is not None:
                raise VerificationError(
                    f"Colliding report group {index} must not have copied_to"
                )
        else:
            if copied_to != expected_name:
                raise VerificationError(f"Incorrect copied_to name in report group {index}")
            normalized_destination = copied_to.casefold()
            if normalized_destination in reported_destinations:
                raise VerificationError(f"Output filename clash: {copied_to}")
            reported_destinations.add(normalized_destination)

            copied_path = config.destination_dir / copied_to
            if copied_path.is_symlink() or not copied_path.is_file():
                raise VerificationError(
                    f"Copied PDF is missing or not a regular file: {copied_path}"
                )
            if hash_file(copied_path) != digest:
                raise VerificationError(f"Copied PDF hash mismatch: {copied_path}")

    actual_sources = set(source_by_relative)
    if reported_sources != actual_sources:
        missing = sorted(actual_sources - reported_sources)
        extra = sorted(reported_sources - actual_sources)
        raise VerificationError(
            f"Source inventory mismatch; omitted={missing[:5]}, unrecognized={extra[:5]}"
        )

    expected_collisions = [
        {
            "copied_to": representatives[0].replace("/", "_"),
            "source_paths": representatives,
        }
        for representatives in representatives_by_name.values()
        if len(representatives) > 1
    ]
    if report.get("collisions") != expected_collisions:
        raise VerificationError("Reported filename collisions do not match the source")
    if report.get("collision_count") != len(expected_collisions):
        raise VerificationError("collision_count does not match the source inventory")
    expected_collision_names = {
        name
        for name, representatives in representatives_by_name.items()
        if len(representatives) > 1
    }
    if any(
        group_collisions[representative]
        != (name in expected_collision_names)
        for name, representatives in representatives_by_name.items()
        for representative in representatives
    ):
        raise VerificationError("Report collision flags do not match the source")
    expected_copied_count = sum(
        len(representatives) == 1
        for representatives in representatives_by_name.values()
    )
    if report.get("copied_pdf_count") != expected_copied_count:
        raise VerificationError("copied_pdf_count does not match collision groups")

    actual_destinations = _destination_files(config.destination_dir)
    expected_destinations = {
        group["copied_to"] for group in groups if group["copied_to"] is not None
    }
    if actual_destinations != expected_destinations:
        extra = sorted(actual_destinations - expected_destinations)
        missing = sorted(expected_destinations - actual_destinations)
        raise VerificationError(
            f"Destination inventory mismatch; extra={extra[:5]}, missing={missing[:5]}"
        )
    if report.get("source_pdf_count") != len(source_paths):
        raise VerificationError("source_pdf_count does not match the source inventory")
    if report.get("unique_count") != len(groups):
        raise VerificationError("unique_count does not match the report groups")
    if report.get("duplicate_count") != len(source_paths) - len(groups):
        raise VerificationError("duplicate_count does not match the source inventory")
    if report.get("source_total_bytes") != sum(source_sizes.values()):
        raise VerificationError("source_total_bytes does not match the source inventory")
    expected_unique_bytes = sum(
        source_sizes[group["copied_from"]] for group in groups
    )
    if report.get("unique_total_bytes") != expected_unique_bytes:
        raise VerificationError("unique_total_bytes does not match the source inventory")
    expected_copied_bytes = sum(
        source_sizes[group["copied_from"]]
        for group in groups
        if not group["filename_collision"]
    )
    if report.get("copied_total_bytes") != expected_copied_bytes:
        raise VerificationError("copied_total_bytes does not match the source inventory")
    return len(groups)


def main() -> int:
    if len(sys.argv) != 1:
        print("This script accepts no command-line arguments.", file=sys.stderr)
        return 2
    try:
        config = load_config(allow_existing_destination=True)
        unique_count = verify_report(config)
    except (ConfigurationError, OSError, ValueError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Verified {unique_count} unique copied PDFs and complete source coverage."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
