#!/usr/bin/env python3
"""Repository identity and canonical Git-blob policy regression tests."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from runtime_state import (  # noqa: E402
    RuntimeStateError,
    canonical_config_path,
    initialize_repository_identity,
    legacy_state_inventory,
    repository_layout,
    repository_status,
    resolve_canonical_policy,
    shared_repository_root,
)


class RepositoryIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.base)
        self.git("config", "user.name", "Orka Test", cwd=self.repo)
        self.git("config", "user.email", "orka@example.invalid", cwd=self.repo)
        config = self.repo / ".orchestration/config.yaml"
        config.parent.mkdir()
        config.write_text("schema_version: 1\nmarker: trusted\n", encoding="utf-8")
        self.git("add", ".", cwd=self.repo)
        self.git("commit", "-q", "-m", "initial policy", cwd=self.repo)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=self.repo)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def initialize(self, repo: Path | None = None, ref: str = "refs/remotes/origin/main") -> dict:
        return initialize_repository_identity(
            repo or self.repo,
            policy_ref=ref,
            policy_path=".orchestration/config.yaml",
        )

    def test_normal_and_linked_worktree_share_identity_and_state_root(self) -> None:
        identity = self.initialize()
        linked = self.base / "linked"
        self.git("worktree", "add", "-q", "-b", "feature", str(linked), cwd=self.repo)

        main = repository_status(self.repo)
        lane = repository_status(linked)
        self.assertEqual(main["repository_uuid"], identity["repository_uuid"])
        self.assertEqual(main["repository_uuid"], lane["repository_uuid"])
        self.assertEqual(main["state_root"], lane["state_root"])
        self.assertEqual(Path(main["state_root"]), (self.repo / ".git/orka-runtime").resolve())

    def test_initialization_is_private_atomic_and_idempotent(self) -> None:
        first = self.initialize()
        second = self.initialize()
        marker = self.repo / ".git/orka-runtime/repository.json"
        self.assertEqual(first, second)
        self.assertEqual(stat.S_IMODE(marker.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertFalse(list(marker.parent.glob("*.tmp")))

    def test_policy_is_resolved_from_exact_ref_not_worktree(self) -> None:
        self.initialize()
        local = self.repo / ".orchestration/config.yaml"
        local.write_text("schema_version: 1\nmarker: unmerged\n", encoding="utf-8")
        snapshot = resolve_canonical_policy(self.repo)
        materialized = canonical_config_path(self.repo)
        self.assertIn(b"marker: trusted", snapshot.content)
        self.assertNotIn(b"unmerged", snapshot.content)
        self.assertEqual(materialized.read_bytes(), snapshot.content)
        status = repository_status(self.repo)
        self.assertEqual(status["canonical_policy"]["commit"], snapshot.commit)
        self.assertEqual(status["canonical_policy"]["blob"], snapshot.blob)
        self.assertEqual(status["canonical_policy"]["sha256"], snapshot.digest)

    def test_captain_preflight_reports_initialized_policy_provenance(self) -> None:
        identity = self.initialize()
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/captain-preflight.py"),
                "--plugin-root",
                str(ROOT),
                "--repo",
                str(self.repo),
                "--host",
                "codex",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertIn(result.returncode, (0, 2), result.stderr)
        report = json.loads(result.stdout)
        provenance = report["repository_identity"]
        self.assertEqual(provenance["mode"], "initialized")
        self.assertEqual(provenance["repository_uuid"], identity["repository_uuid"])
        self.assertEqual(
            provenance["canonical_policy"]["path"], ".orchestration/config.yaml"
        )
        self.assertEqual(len(provenance["canonical_policy"]["sha256"]), 64)

    def test_missing_ref_and_missing_blob_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeStateError, "check-ref-format"):
            self.initialize(ref="refs/heads/main~1")
        with self.assertRaisesRegex(RuntimeStateError, "git rev-parse"):
            self.initialize(ref="refs/remotes/origin/missing")
        self.git("checkout", "-q", "-b", "no-policy", cwd=self.repo)
        self.git("rm", "-q", ".orchestration/config.yaml", cwd=self.repo)
        self.git("commit", "-q", "-m", "remove policy", cwd=self.repo)
        with self.assertRaisesRegex(RuntimeStateError, "git rev-parse"):
            self.initialize(ref="refs/heads/no-policy")

    def test_bare_backed_worktree_stays_inside_bare_common_directory(self) -> None:
        bare = self.base / "one.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(bare), cwd=self.base)
        identity = self.initialize(bare, ref="refs/heads/main")
        linked = self.base / "bare-worktree"
        self.git("--git-dir", str(bare), "worktree", "add", "-q", str(linked), "main", cwd=self.base)

        bare_status = repository_status(bare)
        linked_status = repository_status(linked)
        self.assertEqual(bare_status["repository_uuid"], identity["repository_uuid"])
        self.assertEqual(linked_status["repository_uuid"], identity["repository_uuid"])
        self.assertEqual(Path(bare_status["state_root"]), (bare / "orka-runtime").resolve())
        self.assertEqual(shared_repository_root(linked), bare.resolve())
        self.assertNotEqual(shared_repository_root(linked), bare.parent.resolve())

    def test_sibling_bare_repositories_have_distinct_domains(self) -> None:
        first = self.base / "first.git"
        second = self.base / "second.git"
        for bare in (first, second):
            self.git("clone", "-q", "--bare", str(self.repo), str(bare), cwd=self.base)
        first_id = self.initialize(first, ref="refs/heads/main")
        second_id = self.initialize(second, ref="refs/heads/main")
        self.assertNotEqual(first_id["repository_uuid"], second_id["repository_uuid"])
        self.assertNotEqual(repository_layout(first).state_root, repository_layout(second).state_root)

    def test_copied_identity_fails_common_directory_binding(self) -> None:
        first = self.base / "first.git"
        second = self.base / "second.git"
        for bare in (first, second):
            self.git("clone", "-q", "--bare", str(self.repo), str(bare), cwd=self.base)
        self.initialize(first, ref="refs/heads/main")
        shutil.copytree(first / "orka-runtime", second / "orka-runtime")
        with self.assertRaisesRegex(RuntimeStateError, "common_directory binding"):
            repository_status(second)

    def test_malformed_or_symlinked_identity_fails_closed(self) -> None:
        runtime = self.repo / ".git/orka-runtime"
        runtime.mkdir(mode=0o700)
        marker = runtime / "repository.json"
        marker.write_text("not json", encoding="utf-8")
        marker.chmod(0o600)
        with self.assertRaisesRegex(RuntimeStateError, "malformed"):
            repository_status(self.repo)
        marker.unlink()
        marker.symlink_to(self.repo / ".orchestration/config.yaml")
        with self.assertRaisesRegex(RuntimeStateError, "regular file"):
            repository_status(self.repo)

    def test_symlinked_runtime_directory_is_rejected(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (self.repo / ".git/orka-runtime").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeStateError, "not a real directory"):
            self.initialize()

    def test_ambiguous_bare_parent_state_is_inventory_only(self) -> None:
        bare = self.base / "ambiguous.git"
        self.git("clone", "-q", "--bare", str(self.repo), str(bare), cwd=self.base)
        historical = self.base / ".orchestration"
        historical.mkdir()
        evidence = historical / ".sprint-state/checkpoint.json"
        evidence.parent.mkdir()
        evidence.write_text(json.dumps({"repository": "unknown"}), encoding="utf-8")

        inventory = legacy_state_inventory(bare)
        self.assertEqual(inventory["ambiguous_parent_state"], str(historical.resolve()))
        self.assertTrue(evidence.is_file())
        self.assertFalse((bare / ".orchestration").exists())


if __name__ == "__main__":
    unittest.main()
