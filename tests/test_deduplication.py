import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import deduplicate
from deduplicate import (
    Config,
    ConfigurationError,
    load_config,
    run_deduplication,
)
from verify_deduplication import VerificationError, verify_report


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "recovered"
        self.destination = self.root / "unique"
        self.report = self.root / "report.json"
        self.dry_run_report = self.root / "dry-run-report.json"
        self.hash_cache = self.root / "hashes.sqlite3"
        self.source.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def config(self):
        return Config(
            self.source,
            self.destination,
            self.report,
            self.dry_run_report,
            self.hash_cache,
        )

    def test_deduplicates_deterministically_and_verifies_complete_output(self):
        (self.source / "B").mkdir()
        (self.source / "A").mkdir()
        (self.source / "B" / "x.pdf").write_bytes(b"same content")
        (self.source / "A" / "x.pdf").write_bytes(b"same content")
        (self.source / "only.pdf").write_bytes(b"unique content")
        source_before = {
            path.relative_to(self.source): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.source.rglob("*")
            if path.is_file()
        }
        output = io.StringIO()

        report = run_deduplication(self.config(), output)

        self.assertEqual(report["source_total_bytes"], 38)
        self.assertEqual(report["unique_total_bytes"], 26)
        self.assertEqual(report["copied_total_bytes"], 26)
        self.assertIn("Source: 3 PDFs, 38 B.", output.getvalue())
        self.assertIn("Eligible source: 3 PDFs, 38 B.", output.getvalue())
        self.assertIn("Unique: 2 PDFs, 26 B (would save 12 B).", output.getvalue())
        self.assertIn("Copyable: 2 PDFs, 26 B.", output.getvalue())
        duplicate_group = next(
            group for group in report["groups"] if group["duplicates"]
        )
        self.assertEqual(duplicate_group["copied_from"], "A/x.pdf")
        self.assertEqual(duplicate_group["duplicates"], ["B/x.pdf"])
        self.assertEqual(duplicate_group["copied_to"], "A_x.pdf")
        self.assertTrue((self.destination / "A_x.pdf").is_file())
        self.assertTrue((self.destination / "only.pdf").is_file())
        self.assertIn("remaining 0", output.getvalue())
        self.assertEqual(verify_report(self.config()), 2)
        source_after = {
            path.relative_to(self.source): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.source.rglob("*")
            if path.is_file()
        }
        self.assertEqual(source_before, source_after)

    def test_filename_collision_is_reported_and_other_files_are_copied(self):
        (self.source / "A").mkdir()
        (self.source / "A" / "x.pdf").write_bytes(b"one")
        (self.source / "A_x.pdf").write_bytes(b"two")
        (self.source / "standalone.pdf").write_bytes(b"safe")
        output = io.StringIO()

        report = run_deduplication(self.config(), output)

        self.assertEqual(report["unique_count"], 3)
        self.assertEqual(report["copied_pdf_count"], 1)
        self.assertEqual(report["collision_count"], 1)
        self.assertEqual(
            report["collisions"][0],
            {
                "copied_to": "A_x.pdf",
                "source_paths": ["A/x.pdf", "A_x.pdf"],
            },
        )
        self.assertFalse((self.destination / "A_x.pdf").exists())
        self.assertEqual(
            (self.destination / "standalone.pdf").read_bytes(), b"safe"
        )
        self.assertIn("Filename collisions requiring manual review: 1", output.getvalue())
        self.assertEqual(verify_report(self.config()), 3)

    def test_dry_run_lists_filename_collisions_and_counts_copyable_files(self):
        (self.source / "A").mkdir()
        (self.source / "A" / "x.pdf").write_bytes(b"first")
        (self.source / "A_x.pdf").write_bytes(b"second")
        (self.source / "standalone.pdf").write_bytes(b"safe")
        output = io.StringIO()

        report = run_deduplication(self.config(), output, dry_run=True)

        self.assertEqual(report["unique_count"], 3)
        self.assertEqual(report["copied_pdf_count"], 1)
        self.assertEqual(report["copied_total_bytes"], 4)
        self.assertIn("Filename collisions requiring manual review: 1", output.getvalue())
        self.assertIn("A_x.pdf: A/x.pdf, A_x.pdf", output.getvalue())
        self.assertFalse(self.destination.exists())
        self.assertEqual(
            json.loads(
                self.dry_run_report.read_text(encoding="utf-8")
            )["collision_count"],
            1,
        )

    def test_progress_is_reported_periodically(self):
        for index in range(101):
            (self.source / f"{index:03}.pdf").write_bytes(f"content {index}".encode())

        output = io.StringIO()
        run_deduplication(self.config(), output)

        self.assertIn(
            "Processed 100/101 PDFs; deduplicated 0; remaining 1.",
            output.getvalue(),
        )

    def test_dry_run_reports_start_and_final_progress_for_small_input(self):
        (self.source / "a.pdf").write_bytes(b"same")
        (self.source / "b.pdf").write_bytes(b"same")
        output = io.StringIO()

        report = run_deduplication(self.config(), output, dry_run=True)

        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "Dry run: scanning 2 PDFs...")
        self.assertIn(
            "Processed 2/2 PDFs; deduplicated 1; remaining 0.", lines
        )
        self.assertEqual(report["source_pdf_count"], 2)
        self.assertIn(
            "Hashing complete: 2 PDFs hashed; 1 unique content groups identified; "
            "0 oversized outliers skipped.",
            lines,
        )

    def test_hash_completion_marker_precedes_copying(self):
        (self.source / "a.pdf").write_bytes(b"content")
        output = io.StringIO()

        run_deduplication(self.config(), output)

        lines = output.getvalue().splitlines()
        marker_index = next(
            index for index, line in enumerate(lines) if line.startswith("Hashing complete:")
        )
        copied_index = next(
            index for index, line in enumerate(lines) if line.startswith("Copied ")
        )
        self.assertLess(marker_index, copied_index)

    def test_dry_run_reports_periodic_progress_for_large_input(self):
        for index in range(101):
            (self.source / f"{index:03}.pdf").write_bytes(f"content {index}".encode())
        output = io.StringIO()

        run_deduplication(self.config(), output, dry_run=True)

        self.assertIn(
            "Processed 100/101 PDFs; deduplicated 0; remaining 1.",
            output.getvalue(),
        )
        self.assertIn(
            "Processed 101/101 PDFs; deduplicated 0; remaining 0.",
            output.getvalue(),
        )

    def test_dry_run_reports_counts_and_sizes_without_outputs(self):
        (self.source / "same.pdf").write_bytes(b"same")
        (self.source / "sub").mkdir()
        (self.source / "sub" / "duplicate.pdf").write_bytes(b"same")
        (self.source / "unique.pdf").write_bytes(b"one")

        output = io.StringIO()
        report = run_deduplication(self.config(), output, dry_run=True)

        self.assertEqual(report["source_pdf_count"], 3)
        self.assertEqual(report["unique_count"], 2)
        self.assertEqual(report["source_total_bytes"], 11)
        self.assertEqual(report["unique_total_bytes"], 7)
        self.assertIn("Source: 3 PDFs, 11 B.", output.getvalue())
        self.assertIn("Eligible source: 3 PDFs, 11 B.", output.getvalue())
        self.assertIn(
            "Unique: 2 PDFs, 7 B (would save 4 B).", output.getvalue()
        )
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.report.exists())
        dry_report = json.loads(self.dry_run_report.read_text(encoding="utf-8"))
        self.assertTrue(dry_report["dry_run"])
        self.assertEqual(dry_report["outlier_count"], 0)
        self.assertTrue(self.hash_cache.is_file())

    def test_dry_run_skips_and_reports_files_above_500mb_threshold(self):
        (self.source / "at-limit.pdf").write_bytes(b"x" * 10)
        (self.source / "over-limit.pdf").write_bytes(b"y" * 11)
        output = io.StringIO()

        with patch("deduplicate.MAX_PDF_SIZE_BYTES", 10):
            report = run_deduplication(self.config(), output, dry_run=True)

        self.assertEqual(report["source_pdf_count"], 2)
        self.assertEqual(report["source_total_bytes"], 21)
        self.assertEqual(report["eligible_pdf_count"], 1)
        self.assertEqual(report["eligible_total_bytes"], 10)
        self.assertEqual(report["outlier_count"], 1)
        self.assertEqual(report["outlier_total_bytes"], 11)
        self.assertEqual(
            report["outliers"], [{"path": "over-limit.pdf", "size_bytes": 11}]
        )
        self.assertIn("Skipped oversized PDF outliers (>500 MB): 1 (11 B total).", output.getvalue())
        self.assertIn("  over-limit.pdf: 11 B", output.getvalue())
        self.assertFalse(self.destination.exists())
        dry_report = json.loads(self.dry_run_report.read_text(encoding="utf-8"))
        self.assertTrue(dry_report["dry_run"])
        self.assertEqual(dry_report["outliers"], report["outliers"])
        with deduplicate.sqlite3.connect(self.hash_cache) as db:
            cached = db.execute("SELECT filepath FROM file_hashes").fetchall()
        self.assertEqual(len(cached), 1)
        self.assertTrue(cached[0][0].endswith("at-limit.pdf"))

    def test_verifier_rejects_dry_run_report(self):
        (self.source / "a.pdf").write_bytes(b"document")
        run_deduplication(self.config(), io.StringIO(), dry_run=True)

        with self.assertRaisesRegex(VerificationError, "Dry-run reports"):
            config = self.config()
            verify_report(
                Config(
                    config.source_dir,
                    config.destination_dir,
                    config.dry_run_report_path,
                    config.report_path,
                    config.hash_cache_path,
                )
            )

    def test_dry_run_preserves_normal_report_and_existing_destination(self):
        (self.source / "a.pdf").write_bytes(b"document")
        run_deduplication(self.config(), io.StringIO())
        normal_report_before = self.report.read_bytes()

        run_deduplication(self.config(), io.StringIO(), dry_run=True)

        self.assertEqual(self.report.read_bytes(), normal_report_before)
        self.assertTrue((self.destination / "a.pdf").is_file())
        dry_report = json.loads(self.dry_run_report.read_text(encoding="utf-8"))
        self.assertTrue(dry_report["dry_run"])

    def test_normal_run_skips_outlier_and_verifier_accepts_report(self):
        (self.source / "included.pdf").write_bytes(b"included")
        (self.source / "oversized.pdf").write_bytes(b"z" * 11)
        output = io.StringIO()

        with patch("deduplicate.MAX_PDF_SIZE_BYTES", 10):
            report = run_deduplication(self.config(), output)
            with patch("verify_deduplication.MAX_PDF_SIZE_BYTES", 10):
                self.assertEqual(verify_report(self.config()), 1)

        self.assertEqual(report["outlier_count"], 1)
        self.assertFalse(json.loads(self.report.read_text(encoding="utf-8"))["dry_run"])
        self.assertEqual(report["outliers"], [{"path": "oversized.pdf", "size_bytes": 11}])
        self.assertTrue((self.destination / "included.pdf").is_file())
        self.assertFalse((self.destination / "oversized.pdf").exists())
        self.assertIn("Skipped oversized PDF outliers (>500 MB): 1 (11 B total).", output.getvalue())
        self.assertIn("  oversized.pdf: 11 B", output.getvalue())

    def test_hash_cache_reuses_unchanged_files_and_rehashes_changed_files(self):
        first = self.source / "first.pdf"
        second = self.source / "second.pdf"
        first.write_bytes(b"first")
        second.write_bytes(b"second")

        with patch("deduplicate.hash_file", wraps=deduplicate.hash_file) as hasher:
            run_deduplication(self.config(), io.StringIO(), dry_run=True)
            self.assertEqual(hasher.call_count, 2)
            hasher.reset_mock()
            run_deduplication(self.config(), io.StringIO(), dry_run=True)
            self.assertEqual(hasher.call_count, 0)
            first.write_bytes(b"changed with a different size")
            run_deduplication(self.config(), io.StringIO(), dry_run=True)
            self.assertEqual(hasher.call_count, 1)

    def test_verifier_detects_source_omitted_from_report(self):
        (self.source / "a.pdf").write_bytes(b"a")
        (self.source / "b.pdf").write_bytes(b"b")
        run_deduplication(self.config(), io.StringIO())
        report = json.loads(self.report.read_text(encoding="utf-8"))
        report["groups"].pop()
        report["unique_count"] = 1
        report["duplicate_count"] = 1
        self.report.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaisesRegex(VerificationError, "Source inventory mismatch"):
            verify_report(self.config())

    def test_verifier_detects_unreported_destination_file(self):
        (self.source / "a.pdf").write_bytes(b"a")
        run_deduplication(self.config(), io.StringIO())
        (self.destination / "extra.pdf").write_bytes(b"extra")

        with self.assertRaisesRegex(VerificationError, "Destination inventory mismatch"):
            verify_report(self.config())

    def test_sha256_report_digest_matches_content(self):
        content = b"pdf bytes"
        (self.source / "a.pdf").write_bytes(content)

        report = run_deduplication(self.config(), io.StringIO())

        self.assertEqual(report["groups"][0]["sha256"], hashlib.sha256(content).hexdigest())

    def test_human_size_format_uses_binary_units(self):
        self.assertEqual(deduplicate.format_size(1536), "1.5 KiB")
        self.assertEqual(deduplicate.format_size(1024 * 1024), "1.0 MiB")

    def test_cwd_config_supports_both_script_runs(self):
        config_path = self.root / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "source_dir": "recovered",
                    "destination_dir": "configured-output",
                    "hash_cache_path": "configured-cache.sqlite3",
                }
            ),
            encoding="utf-8",
        )
        (self.source / "a.pdf").write_bytes(b"a")

        config = load_config(self.root)
        self.assertEqual(
            config.hash_cache_path, self.root.resolve() / "configured-cache.sqlite3"
        )
        self.assertEqual(
            config.dry_run_report_path,
            self.root.resolve() / "deduplication_dry_run_report.json",
        )
        run_deduplication(config, io.StringIO())
        verifier_config = load_config(self.root, allow_existing_destination=True)

        self.assertEqual(verify_report(verifier_config), 1)
        with self.assertRaises(ConfigurationError):
            load_config(self.root)


if __name__ == "__main__":
    unittest.main()
