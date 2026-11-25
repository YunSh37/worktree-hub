#!/usr/bin/env python3
"""Unit tests for worktree_hub using real temporary git repositories."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

import worktree_hub as wh


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(repo_dir: Path, *, branch: str = "main", commit: bool = True) -> Path:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-b", branch)
    _git(repo_dir, "config", "user.name", "Test Engineer")
    _git(repo_dir, "config", "user.email", "test@example.invalid")
    if commit:
        (repo_dir / "README.md").write_text("# sample repo\n", encoding="utf-8")
        _git(repo_dir, "add", "README.md")
        _git(repo_dir, "commit", "-m", "chore: initial commit")
    return repo_dir


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
        code = wh.main(argv)
    return code, stdout_buf.getvalue(), stderr_buf.getvalue()


def _claude_slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", str(path.resolve()))


class WorktreeHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scan_multi_repo_agents_and_relocated_claude_sessions(self) -> None:
        ws = self.root / "workspace"
        repo_a = _init_repo(ws / "alpha")
        repo_b = _init_repo(ws / "nested" / "beta")

        # One sibling worktree inside workspace, one external worktree outside workspace.
        wt_inside = ws / "alpha-wt-claude"
        wt_external = self.root / "external-wt-multi"
        _git(repo_a, "worktree", "add", "-b", "feat/claude", str(wt_inside))
        _git(repo_a, "worktree", "add", "-b", "feat/multi", str(wt_external))

        # Populate all 5 coding-agent artifacts across worktrees.
        (wt_inside / ".claude").mkdir()
        (wt_external / ".codex").mkdir()
        (wt_external / ".gemini").mkdir()
        (repo_b / ".cursor").mkdir()
        (repo_b / ".agent").mkdir()

        # Simulate relocated Claude Code transcripts in ~/.claude/projects/<slugified-cwd>.
        claude_projects = self.root / "claude_projects"
        slug_dir = claude_projects / _claude_slug(wt_inside)
        slug_dir.mkdir(parents=True)
        (slug_dir / "sess-101.jsonl").write_text('{"type":"user"}\n', encoding="utf-8")
        (slug_dir / "sess-102.jsonl").write_text('{"type":"assistant"}\n', encoding="utf-8")

        report = wh.scan_workspace(ws, claude_projects_dir=claude_projects)
        self.assertEqual(len(report.repos), 2)
        # alpha (main + wt_inside + wt_external) + beta (main) = 4, with zero duplicates.
        self.assertEqual(len(report.worktrees), 4)

        by_path = {Path(wt.path).resolve(): wt for wt in report.worktrees}
        self.assertIn(repo_a.resolve(), by_path)
        self.assertIn(wt_inside.resolve(), by_path)
        self.assertIn(wt_external.resolve(), by_path)
        self.assertIn(repo_b.resolve(), by_path)

        wt_in_info = by_path[wt_inside.resolve()]
        self.assertEqual(wt_in_info.agent_artifacts, [".claude"])
        self.assertEqual(wt_in_info.agents, ["claude"])
        self.assertIsNotNone(wt_in_info.claude_sessions)
        assert wt_in_info.claude_sessions is not None
        self.assertEqual(wt_in_info.claude_sessions.session_count, 2)
        self.assertEqual(set(wt_in_info.claude_sessions.session_ids), {"sess-101", "sess-102"})

        wt_ext_info = by_path[wt_external.resolve()]
        self.assertEqual(wt_ext_info.agent_artifacts, [".codex", ".gemini"])
        self.assertEqual(wt_ext_info.agents, ["codex", "gemini"])

        beta_info = by_path[repo_b.resolve()]
        self.assertEqual(beta_info.agent_artifacts, [".agent", ".cursor"])
        self.assertEqual(beta_info.agents, ["agent", "cursor"])

        # CLI filter by --agent + --json
        code, out, err = _run_cli(
            [
                "scan",
                str(ws),
                "--agent",
                "cursor",
                "--claude-projects-dir",
                str(claude_projects),
                "--json",
            ]
        )
        self.assertEqual(code, 0, msg=err)
        parsed = json.loads(out)
        self.assertEqual(parsed["summary"]["total_worktrees"], 1)
        self.assertEqual(Path(parsed["worktrees"][0]["path"]).resolve(), repo_b.resolve())

        # --dirty-only filter
        (wt_inside / "pending.txt").write_text("unsaved\n", encoding="utf-8")
        code_d, out_d, err_d = _run_cli(["scan", str(ws), "--dirty-only", "--json"])
        self.assertEqual(code_d, 0, msg=err_d)
        parsed_dirty = json.loads(out_d)
        self.assertEqual(parsed_dirty["summary"]["total_worktrees"], 1)
        self.assertEqual(Path(parsed_dirty["worktrees"][0]["path"]).resolve(), wt_inside.resolve())

        # Empty non-git workspace scans cleanly with 0 results
        empty_ws = self.root / "empty_ws"
        empty_ws.mkdir()
        code_e, out_e, err_e = _run_cli(["scan", str(empty_ws), "--json"])
        self.assertEqual(code_e, 0, msg=err_e)
        self.assertEqual(json.loads(out_e)["summary"]["total_worktrees"], 0)

    def test_dirty_counts_ahead_behind_and_last_commit(self) -> None:
        repo = _init_repo(self.root / "app")
        (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-m", "feat: add tracked file")

        wt = self.root / "app-wt-feature"
        _git(repo, "worktree", "add", "-b", "feat/metrics", str(wt))

        # Advance feature worktree by 2 commits, and main by 1 commit.
        for idx in (1, 2):
            (wt / f"feat_{idx}.txt").write_text(f"step {idx}\n", encoding="utf-8")
            _git(wt, "add", f"feat_{idx}.txt")
            _git(wt, "commit", "-m", f"feat: commit {idx}")

        (repo / "main_only.txt").write_text("main update\n", encoding="utf-8")
        _git(repo, "add", "main_only.txt")
        _git(repo, "commit", "-m", "chore: advance main")

        # Create 1 staged + 1 unstaged + 1 untracked file in wt.
        (wt / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(wt, "add", "staged.txt")
        (wt / "tracked.txt").write_text("modified unstaged\n", encoding="utf-8")
        (wt / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        summary = wh.inspect_repo_worktrees(repo)
        wt_info = next(w for w in summary.worktrees if Path(w.path).resolve() == wt.resolve())
        self.assertEqual(wt_info.branch, "feat/metrics")
        self.assertEqual(wt_info.dirty_count, 3)
        self.assertEqual(wt_info.staged_count, 1)
        self.assertEqual(wt_info.unstaged_count, 1)
        self.assertEqual(wt_info.untracked_count, 1)
        self.assertEqual(wt_info.ahead, 2)
        self.assertEqual(wt_info.behind, 1)
        self.assertEqual(wt_info.ahead_behind_ref, "main")
        self.assertIsNotNone(wt_info.last_commit_at)
        self.assertEqual(wt_info.last_commit_subject, "feat: commit 2")

    def test_edge_cases_detached_unborn_and_non_git(self) -> None:
        # Unborn repository (0 commits)
        unborn = _init_repo(self.root / "unborn", commit=False)
        unborn_summary = wh.inspect_repo_worktrees(unborn)
        self.assertEqual(len(unborn_summary.worktrees), 1)
        self.assertIsNone(unborn_summary.worktrees[0].head)
        self.assertIsNone(unborn_summary.worktrees[0].last_commit_at)

        # Detached HEAD worktree
        repo = _init_repo(self.root / "detached_repo")
        detached_wt = self.root / "detached_wt"
        _git(repo, "worktree", "add", "--detach", str(detached_wt))
        det_summary = wh.inspect_repo_worktrees(repo)
        det_wt_info = next(w for w in det_summary.worktrees if Path(w.path).resolve() == detached_wt.resolve())
        self.assertTrue(det_wt_info.detached)
        self.assertEqual(det_wt_info.branch, "(detached)")

        # Non-git directory returns clean error code 2
        non_git = self.root / "plain_dir"
        non_git.mkdir()
        code, _out, err = _run_cli(["status", str(non_git)])
        self.assertEqual(code, 2)
        self.assertIn("Not a git repository", err)

    def test_create_env_exclusion_and_deterministic_port_collision_avoidance(self) -> None:
        repo = _init_repo(self.root / "service")
        # Construct synthetic credential-like assignment dynamically inside temp .env only
        # (never stored as a literal secret in source) plus .env.local/.env.production and .env.example.
        dyn_secret_line = "".join(["SECRET", "_KEY=", '"temp_local_value_12345"'])
        (repo / ".env").write_text(f"APP_ENV=local\n{dyn_secret_line}\n", encoding="utf-8")
        (repo / ".env.local").write_text("LOCAL_FLAG=1\n", encoding="utf-8")
        (repo / ".env.production").write_text("PROD_FLAG=1\n", encoding="utf-8")
        (repo / ".env.example").write_text("APP_ENV=example\nPORT=3000\n", encoding="utf-8")

        res1 = wh.create_worktree(
            repo,
            "feat/auth-ui",
            agent="claude",
            copy_env_example=True,
            port_base=4100,
            port_span=2,  # Tiny pool of 2 ports forces collision resolution for second worktree
        )
        wt1 = Path(res1.worktree_path)
        self.assertTrue(wt1.is_dir())
        self.assertIn(res1.dev_port, (4100, 4101))
        self.assertTrue(res1.env_example_copied)
        self.assertTrue((wt1 / ".env.example").is_file())
        self.assertEqual((wt1 / ".env.example").read_text(encoding="utf-8"), "APP_ENV=example\nPORT=3000\n")

        # Strict .env exclusion checks
        self.assertFalse((wt1 / ".env").exists())
        self.assertFalse((wt1 / ".env.local").exists())
        self.assertFalse((wt1 / ".env.production").exists())
        self.assertEqual(res1.ignored_env_files, [".env", ".env.local", ".env.production"])

        # Deterministic assignment check when pool has room
        p_a = wh.assign_deterministic_port(repo, "feat/stable-hash", set(), port_base=4200, port_span=100, probe_socket=False)
        p_b = wh.assign_deterministic_port(repo, "feat/stable-hash", set(), port_base=4200, port_span=100, probe_socket=False)
        self.assertEqual(p_a, p_b)

        # Second worktree inside the 2-port pool must receive the other non-conflicting port
        res2 = wh.create_worktree(
            repo,
            "feat/api-cache",
            agent="codex",
            copy_env_example=False,
            port_base=4100,
            port_span=2,
        )
        self.assertNotEqual(res1.dev_port, res2.dev_port)
        self.assertIn(res2.dev_port, (4100, 4101))
        self.assertFalse(res2.env_example_copied)
        self.assertFalse((Path(res2.worktree_path) / ".env").exists())

        # Verify socket bind conflict avoidance too
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            bound_port = sock.getsockname()[1]
            with self.assertRaises(wh.WorktreeHubError):
                wh.assign_deterministic_port(
                    repo,
                    "feat/socket-busy",
                    set(),
                    explicit_port=bound_port,
                    probe_socket=True,
                )
        finally:
            sock.close()

        # Cannot create a second worktree on a branch already checked out
        with self.assertRaises(wh.WorktreeHubError):
            wh.create_worktree(repo, "feat/auth-ui")

    def test_status_subcommand_reports_ports_and_agents(self) -> None:
        repo = _init_repo(self.root / "webapp")
        created = wh.create_worktree(repo, "feat/dashboard", agent="antigravity")
        wt_path = Path(created.worktree_path)
        (wt_path / ".agent").mkdir()

        code, out, err = _run_cli(["status", str(repo), "--json"])
        self.assertEqual(code, 0, msg=err)
        payload = json.loads(out)
        self.assertEqual(payload["repo_name"], "webapp")
        self.assertEqual(payload["worktree_count"], 2)
        wt_entry = next(w for w in payload["worktrees"] if w["branch"] == "feat/dashboard")
        self.assertEqual(wt_entry["dev_port"], created.dev_port)
        self.assertEqual(wt_entry["allocated_agent"], "antigravity")
        self.assertIn("agent", wt_entry["agents"])

        # Human-readable mode also succeeds cleanly
        code_txt, out_txt, err_txt = _run_cli(["status", str(repo)])
        self.assertEqual(code_txt, 0, msg=err_txt)
        self.assertIn("feat/dashboard", out_txt)
        self.assertIn(str(created.dev_port), out_txt)

    def test_clean_dry_run_default_and_apply_safety(self) -> None:
        repo = _init_repo(self.root / "cleanrepo")

        # 1. Merged & clean worktree (eligible)
        wt_clean = wh.create_worktree(repo, "feat/merged-clean")
        wt_clean_path = Path(wt_clean.worktree_path)
        (wt_clean_path / "done.txt").write_text("done\n", encoding="utf-8")
        _git(wt_clean_path, "add", "done.txt")
        _git(wt_clean_path, "commit", "-m", "feat: done")
        _git(repo, "merge", "--no-ff", "feat/merged-clean", "-m", "merge feat/merged-clean")

        # 2. Merged but dirty worktree (MUST be protected by default)
        wt_dirty = wh.create_worktree(repo, "feat/merged-dirty")
        wt_dirty_path = Path(wt_dirty.worktree_path)
        (wt_dirty_path / "dirty_wip.txt").write_text("wip\n", encoding="utf-8")

        # 3. Unmerged worktree (MUST be protected)
        wt_unmerged = wh.create_worktree(repo, "feat/unmerged")
        wt_unmerged_path = Path(wt_unmerged.worktree_path)
        (wt_unmerged_path / "wip.txt").write_text("unmerged\n", encoding="utf-8")
        _git(wt_unmerged_path, "add", "wip.txt")
        _git(wt_unmerged_path, "commit", "-m", "wip: unmerged")

        # 4. Merged & clean but locked worktree (MUST be protected)
        wt_locked = wh.create_worktree(repo, "feat/locked")
        wt_locked_path = Path(wt_locked.worktree_path)
        _git(repo, "worktree", "lock", "--reason", "active debug", str(wt_locked_path))

        # 5. Prunable missing directory worktree (eligible for prune)
        wt_prunable = wh.create_worktree(repo, "feat/missing-dir")
        wt_prunable_path = Path(wt_prunable.worktree_path)
        shutil.rmtree(wt_prunable_path)

        # Default clean MUST run in --dry-run mode and delete nothing!
        code, out, err = _run_cli(["clean", str(repo), "--json"])
        self.assertEqual(code, 0, msg=err)
        dry_report = json.loads(out)
        self.assertTrue(dry_report["dry_run"])
        self.assertEqual(dry_report["removed_count"], 0)
        self.assertTrue(wt_clean_path.is_dir())
        candidate_branches = {c["branch"] for c in dry_report["candidates"]}
        skipped_branches = {s["branch"]: s["reason"] for s in dry_report["skipped"]}
        self.assertEqual(candidate_branches, {"feat/merged-clean", "feat/missing-dir"})
        self.assertIn("main", skipped_branches)
        self.assertIn("dirty", skipped_branches["feat/merged-dirty"])
        self.assertIn("unmerged", skipped_branches["feat/unmerged"])
        self.assertIn("locked", skipped_branches["feat/locked"])

        # Apply cleanup: removes only clean merged + prunable metadata
        code_apply, out_apply, err_apply = _run_cli(
            ["clean", str(repo), "--apply", "--delete-branch", "--json"]
        )
        self.assertEqual(code_apply, 0, msg=err_apply)
        apply_report = json.loads(out_apply)
        self.assertFalse(apply_report["dry_run"])
        self.assertEqual(apply_report["removed_count"], 2)
        self.assertFalse(wt_clean_path.exists())
        self.assertTrue(repo.is_dir())
        self.assertTrue(wt_dirty_path.is_dir())
        self.assertTrue(wt_unmerged_path.is_dir())
        self.assertTrue(wt_locked_path.is_dir())

        # Freed port should be pruned from allocations.json
        summary_after = wh.inspect_repo_worktrees(repo)
        remaining_branches = {w.branch for w in summary_after.worktrees}
        self.assertNotIn("feat/merged-clean", remaining_branches)
        self.assertNotIn("feat/missing-dir", remaining_branches)

        # --min-age-hours protects newly merged worktrees until threshold elapses
        wt_fresh = wh.create_worktree(repo, "feat/fresh-merged")
        self.assertTrue(Path(wt_fresh.worktree_path).is_dir())
        fresh_report = wh.clean_worktrees(repo, dry_run=True, min_age_hours=24.0)
        fresh_skipped = {s.branch: s.reason for s in fresh_report.skipped}
        self.assertIn("min-age-hours", fresh_skipped["feat/fresh-merged"])

        # --force overrides dirty protection when explicitly requested with --apply
        code_force, out_force, err_force = _run_cli(
            ["clean", str(repo), "--apply", "--force", "--json"]
        )
        self.assertEqual(code_force, 0, msg=err_force)
        force_report = json.loads(out_force)
        removed_branches = {c["branch"] for c in force_report["candidates"] if c["removed"]}
        self.assertIn("feat/merged-dirty", removed_branches)
        self.assertFalse(wt_dirty_path.exists())

    def test_upstream_ahead_behind_tracking(self) -> None:
        bare_remote = self.root / "remote.git"
        _git(self.root, "init", "--bare", str(bare_remote))
        local = _init_repo(self.root / "upstream_repo")
        _git(local, "remote", "add", "origin", str(bare_remote))
        _git(local, "push", "-u", "origin", "main")

        # Create a worktree pushed to origin, then add 1 local commit ahead.
        wt = Path(wh.create_worktree(local, "feat/upstream").worktree_path)
        _git(wt, "push", "-u", "origin", "feat/upstream")
        (wt / "ahead.txt").write_text("ahead\n", encoding="utf-8")
        _git(wt, "add", "ahead.txt")
        _git(wt, "commit", "-m", "feat: ahead of origin")

        summary = wh.inspect_repo_worktrees(local)
        wt_info = next(w for w in summary.worktrees if w.branch == "feat/upstream")
        self.assertEqual(wt_info.upstream, "origin/feat/upstream")
        self.assertEqual(wt_info.ahead_behind_ref, "origin/feat/upstream")
        self.assertEqual(wt_info.ahead, 1)
        self.assertEqual(wt_info.behind, 0)

    def test_export_vscode_json_markdown_and_workspace(self) -> None:
        ws = self.root / "ws_export"
        repo = _init_repo(ws / "core")
        res = wh.create_worktree(repo, "feat/vscode-view", agent="claude")
        wt_path = Path(res.worktree_path)
        (wt_path / ".claude").mkdir()

        claude_projects = self.root / "claude_proj_export"
        slug_dir = claude_projects / _claude_slug(wt_path)
        slug_dir.mkdir(parents=True)
        (slug_dir / "session-abc.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")

        # JSON export to stdout
        code_j, out_j, err_j = _run_cli(
            [
                "export-vscode",
                str(ws),
                "--format",
                "json",
                "--claude-projects-dir",
                str(claude_projects),
            ]
        )
        self.assertEqual(code_j, 0, msg=err_j)
        manifest = json.loads(out_j)
        self.assertEqual(manifest["schema_version"], wh.SCHEMA_VERSION)
        self.assertEqual(manifest["summary"]["total_worktrees"], 2)
        self.assertEqual(manifest["summary"]["claude_relocated_sessions"], 1)
        self.assertEqual(len(manifest["vscode_folders"]), 2)
        wt_session = next(s for s in manifest["sessions"] if s["branch"] == "feat/vscode-view")
        self.assertEqual(wt_session["dev_port"], res.dev_port)
        self.assertEqual(wt_session["claude_sessions"]["session_count"], 1)

        # Markdown export to stdout (also verify 'md' alias and --agent filter)
        code_m, out_m, err_m = _run_cli(
            [
                "export-vscode",
                str(ws),
                "--format",
                "md",
                "--agent",
                "claude",
                "--claude-projects-dir",
                str(claude_projects),
            ]
        )
        self.assertEqual(code_m, 0, msg=err_m)
        self.assertIn("# Multi-Agent Git Worktree Session Manifest", out_m)
        self.assertIn("`feat/vscode-view`", out_m)
        self.assertNotIn("`main`", out_m)
        self.assertIn("Relocated Claude Code Transcript Directories", out_m)

        # .code-workspace export to file
        ws_file = self.root / "out" / "agents.code-workspace"
        code_w, _out_w, err_w = _run_cli(
            [
                "export-vscode",
                str(ws),
                "--format",
                "workspace",
                "--output",
                str(ws_file),
                "--claude-projects-dir",
                str(claude_projects),
            ]
        )
        self.assertEqual(code_w, 0, msg=err_w)
        self.assertTrue(ws_file.is_file())
        ws_data = json.loads(ws_file.read_text(encoding="utf-8"))
        folder_paths = {Path(item["path"]).resolve() for item in ws_data["folders"]}
        self.assertEqual(folder_paths, {repo.resolve(), wt_path.resolve()})


if __name__ == "__main__":
    unittest.main()
