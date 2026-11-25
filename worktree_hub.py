#!/usr/bin/env python3
"""Zero-dependency multi-agent git worktree hub CLI.

Solves worktree and session fragmentation across Claude Code, Codex, Antigravity,
Cursor, and VS Code (inspired by anthropics/claude-code#81024):
- Discovers all git worktrees across a workspace/root along with active agent
  artifacts (.claude, .codex, .gemini, .cursor, .agent) and relocated Claude
  Code session transcripts (~/.claude/projects/<slugified-cwd>/).
- Safely provisions isolated worktrees with deterministic non-conflicting dev
  ports and optional .env.example template copying (never copies raw .env).
- Previews (--dry-run default) and safely cleans stale merged worktrees.
- Exports consolidated JSON, Markdown, or .code-workspace manifests so all
  parallel worktree sessions stay visible from a single view.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

__version__ = "0.1.0"
SCHEMA_VERSION = "1.0"

AGENT_ARTIFACT_MAP: dict[str, str] = {
    ".claude": "claude",
    ".codex": "codex",
    ".gemini": "gemini",
    ".cursor": "cursor",
    ".agent": "agent",
}

EXCLUDED_SCAN_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", ".tmp", "dist", "build"}
)

DEFAULT_PORT_BASE = 3100
DEFAULT_PORT_SPAN = 900
DEFAULT_MAX_DEPTH = 4
GIT_TIMEOUT_SEC = 15

_SLUG_STRICT_RE = re.compile(r"[^A-Za-z0-9-]")
_SLUG_KEEP_UNDERSCORE_RE = re.compile(r"[^A-Za-z0-9_-]")


class WorktreeHubError(Exception):
    """Domain error raised for invalid repositories, git failures, or unsafe ops."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclasses.dataclass(frozen=True)
class ClaudeSessionInfo:
    """Metadata for relocated Claude Code session transcripts on disk."""

    project_dir: str
    session_count: int
    latest_session_at: str | None
    session_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class WorktreeInfo:
    """Rich inspection state for a single git worktree."""

    repo_name: str
    repo_root: str
    path: str
    is_main: bool
    exists: bool
    head: str | None
    head_short: str | None
    branch: str
    detached: bool
    bare: bool
    locked: bool
    lock_reason: str
    prunable: bool
    prune_reason: str
    dirty_count: int
    staged_count: int
    unstaged_count: int
    untracked_count: int
    ahead: int
    behind: int
    upstream: str | None
    ahead_behind_ref: str | None
    merged_into_base: bool
    last_commit_at: str | None
    last_commit_subject: str | None
    agent_artifacts: list[str]
    agents: list[str]
    dev_port: int | None
    allocated_agent: str | None
    claude_sessions: ClaudeSessionInfo | None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        return data


@dataclasses.dataclass
class RepoSummary:
    """Consolidated worktree inventory for a single git repository family."""

    repo_name: str
    repo_root: str
    git_common_dir: str
    base_branch: str | None
    worktrees: list[WorktreeInfo]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "repo_root": self.repo_root,
            "git_common_dir": self.git_common_dir,
            "base_branch": self.base_branch,
            "worktree_count": len(self.worktrees),
            "worktrees": [wt.to_dict() for wt in self.worktrees],
        }


@dataclasses.dataclass
class ScanReport:
    """Multi-repo workspace scan result."""

    schema_version: str
    generated_at: str
    root: str
    repos: list[RepoSummary]
    worktrees: list[WorktreeInfo]

    def to_dict(self) -> dict[str, Any]:
        agents_detected = sorted({agent for wt in self.worktrees for agent in wt.agents})
        relocated_sessions = sum(
            wt.claude_sessions.session_count for wt in self.worktrees if wt.claude_sessions
        )
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "root": self.root,
            "summary": {
                "total_repos": len(self.repos),
                "total_worktrees": len(self.worktrees),
                "dirty_worktrees": sum(1 for wt in self.worktrees if wt.dirty_count > 0),
                "active_agent_worktrees": sum(1 for wt in self.worktrees if wt.agents),
                "claude_relocated_sessions": relocated_sessions,
                "agents_detected": agents_detected,
            },
            "repos": [repo.to_dict() for repo in self.repos],
            "worktrees": [wt.to_dict() for wt in self.worktrees],
        }


@dataclasses.dataclass
class CreateResult:
    """Result summary after provisioning an isolated worktree."""

    repo_root: str
    worktree_path: str
    branch: str
    base_ref: str | None
    dev_port: int
    agent: str | None
    env_example_copied: bool
    env_example_status: str
    ignored_env_files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CleanCandidate:
    """A stale merged or prunable worktree selected for removal."""

    repo_name: str
    path: str
    branch: str
    reason: str
    prunable: bool
    merged_into_base: bool
    dirty_count: int
    removed: bool = False
    branch_deleted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CleanSkipped:
    """A worktree intentionally preserved by cleanup safety checks."""

    path: str
    branch: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CleanReport:
    """Summary of dry-run preview or applied cleanup execution."""

    repo_root: str
    base_branch: str | None
    dry_run: bool
    candidates: list[CleanCandidate]
    skipped: list[CleanSkipped]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "repo_root": self.repo_root,
            "base_branch": self.base_branch,
            "dry_run": self.dry_run,
            "candidate_count": len(self.candidates),
            "removed_count": sum(1 for item in self.candidates if item.removed),
            "candidates": [item.to_dict() for item in self.candidates],
            "skipped": [item.to_dict() for item in self.skipped],
            "errors": self.errors,
        }


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _run_git(
    args: Sequence[str],
    cwd: Path,
    *,
    check: bool = True,
    timeout: int = GIT_TIMEOUT_SEC,
) -> subprocess.CompletedProcess[str]:
    """Execute git safely without shell interpolation."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeHubError("git executable not found on PATH", exit_code=2) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeHubError(f"git command timed out: git {' '.join(args)}", exit_code=1) from exc

    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
        raise WorktreeHubError(f"git {' '.join(args)} failed: {detail}", exit_code=1)
    return proc


def _resolve_git_path(base_dir: Path, raw_path: str) -> Path:
    candidate = Path(raw_path.strip())
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def resolve_repo_context(target: Path) -> tuple[Path, Path]:
    """Return (top_level_or_dir, git_common_dir) for a git repo or worktree."""
    resolved = target.resolve()
    if not resolved.exists():
        raise WorktreeHubError(f"Path does not exist: {target}", exit_code=2)
    probe_dir = resolved if resolved.is_dir() else resolved.parent

    common_proc = _run_git(["rev-parse", "--git-common-dir"], cwd=probe_dir, check=False)
    if common_proc.returncode != 0:
        raise WorktreeHubError(f"Not a git repository: {target}", exit_code=2)

    common_dir = _resolve_git_path(probe_dir, common_proc.stdout)

    top_proc = _run_git(["rev-parse", "--show-toplevel"], cwd=probe_dir, check=False)
    if top_proc.returncode == 0 and top_proc.stdout.strip():
        top_dir = _resolve_git_path(probe_dir, top_proc.stdout)
    else:
        top_dir = common_dir.parent if common_dir.name == ".git" else common_dir

    return top_dir, common_dir


def discover_git_repos(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> list[tuple[Path, Path]]:
    """Discover unique (repo_probe_dir, git_common_dir) pairs under a root folder."""
    resolved_root = root.resolve()
    if not resolved_root.exists():
        raise WorktreeHubError(f"Scan root does not exist: {root}", exit_code=2)
    if not resolved_root.is_dir():
        raise WorktreeHubError(f"Scan root is not a directory: {root}", exit_code=2)

    found: dict[Path, Path] = {}

    # If root itself is inside an enclosing git repository, capture it first.
    probe = _run_git(["rev-parse", "--git-common-dir"], cwd=resolved_root, check=False)
    if probe.returncode == 0 and probe.stdout.strip():
        common_dir = _resolve_git_path(resolved_root, probe.stdout)
        found[common_dir] = resolved_root

    root_depth = len(resolved_root.parts)
    for current_str, dirnames, filenames in os.walk(resolved_root):
        current = Path(current_str)
        depth = len(current.parts) - root_depth

        if ".git" in dirnames or ".git" in filenames:
            git_probe = _run_git(["rev-parse", "--git-common-dir"], cwd=current, check=False)
            if git_probe.returncode == 0 and git_probe.stdout.strip():
                common_dir = _resolve_git_path(current, git_probe.stdout)
                found.setdefault(common_dir, current)

        if depth >= max_depth:
            dirnames[:] = []
            continue

        dirnames[:] = sorted(
            d for d in dirnames if d not in EXCLUDED_SCAN_DIRS and not d.startswith(".git")
        )

    return sorted(((probe_dir, common_dir) for common_dir, probe_dir in found.items()), key=lambda x: str(x[1]))


def parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    """Parse `git worktree list --porcelain` stanzas into structured dicts."""
    stanzas: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in output.splitlines():
        if not line.strip():
            if current is not None:
                stanzas.append(current)
                current = None
            continue

        if line.startswith("worktree "):
            if current is not None:
                stanzas.append(current)
            raw_wt = line[len("worktree ") :].strip()
            current = {
                "path": Path(raw_wt).resolve(),
                "head": None,
                "branch": None,
                "detached": False,
                "bare": False,
                "locked": False,
                "lock_reason": "",
                "prunable": False,
                "prune_reason": "",
            }
            continue

        if current is None:
            continue

        if line.startswith("HEAD "):
            sha = line[len("HEAD ") :].strip()
            current["head"] = None if set(sha) == {"0"} else sha
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            if ref.startswith("refs/heads/"):
                ref = ref[len("refs/heads/") :]
            current["branch"] = ref
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
            current["lock_reason"] = line[len("locked") :].strip()
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable"] = True
            current["prune_reason"] = line[len("prunable") :].strip()

    if current is not None:
        stanzas.append(current)
    return stanzas


def _allocation_file(common_dir: Path) -> Path:
    return common_dir / "worktree-hub" / "allocations.json"


def _load_allocations(common_dir: Path) -> dict[str, dict[str, Any]]:
    path = _allocation_file(common_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("worktrees", {})
    if not isinstance(entries, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, val in entries.items():
        if isinstance(val, dict):
            normalized[str(Path(key).resolve())] = val
    return normalized


def _save_allocations(common_dir: Path, entries: dict[str, dict[str, Any]]) -> None:
    path = _allocation_file(common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _utc_now_iso(),
        "worktrees": entries,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _is_port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def assign_deterministic_port(
    repo_root: Path,
    branch: str,
    used_ports: set[int],
    *,
    port_base: int = DEFAULT_PORT_BASE,
    port_span: int = DEFAULT_PORT_SPAN,
    explicit_port: int | None = None,
    probe_socket: bool = True,
) -> int:
    """Assign a deterministic, non-conflicting localhost dev port."""
    if explicit_port is not None:
        if not (1024 <= explicit_port <= 65535):
            raise WorktreeHubError(f"Port must be between 1024 and 65535 (got {explicit_port})", exit_code=2)
        if explicit_port in used_ports:
            raise WorktreeHubError(f"Port {explicit_port} is already allocated to another worktree", exit_code=1)
        if probe_socket and not _is_port_free(explicit_port):
            raise WorktreeHubError(f"Port {explicit_port} is currently bound on 127.0.0.1", exit_code=1)
        return explicit_port

    if port_base < 1024 or port_span < 1 or (port_base + port_span - 1) > 65535:
        raise WorktreeHubError("Invalid port range configuration", exit_code=2)

    digest = hashlib.sha256(f"{repo_root.resolve()}::{branch}".encode("utf-8")).digest()
    start_offset = int.from_bytes(digest[:4], "big") % port_span

    for step in range(port_span):
        candidate = port_base + ((start_offset + step) % port_span)
        if candidate in used_ports:
            continue
        if probe_socket and not _is_port_free(candidate):
            continue
        return candidate

    raise WorktreeHubError(
        f"No free port available in range {port_base}-{port_base + port_span - 1}", exit_code=1
    )


def _inspect_dirty_counts(wt_path: Path) -> tuple[int, int, int, int]:
    """Return (total_dirty, staged, unstaged, untracked) from git status."""
    proc = _run_git(["status", "--porcelain=v1"], cwd=wt_path, check=False)
    if proc.returncode != 0:
        return 0, 0, 0, 0

    staged = 0
    unstaged = 0
    untracked = 0
    total = 0

    for line in proc.stdout.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        total += 1
        if code == "??":
            untracked += 1
            continue
        if code[0] != " ":
            staged += 1
        if code[1] != " ":
            unstaged += 1

    return total, staged, unstaged, untracked


def _inspect_last_commit(wt_path: Path) -> tuple[str | None, str | None]:
    """Return (iso_timestamp, subject) for HEAD commit, or (None, None) if unborn."""
    proc = _run_git(["log", "-1", "--format=%cI%x1f%s"], cwd=wt_path, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, None
    parts = proc.stdout.rstrip("\r\n").split("\x1f", 1)
    committed_at = parts[0].strip() or None
    subject = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return committed_at, subject


def _inspect_ahead_behind(
    wt_path: Path,
    branch: str,
    base_branch: str | None,
    *,
    is_main: bool,
    has_head: bool,
) -> tuple[int, int, str | None, str | None]:
    """Return (ahead, behind, upstream_ref, comparison_ref)."""
    if not has_head:
        return 0, 0, None, None

    upstream_proc = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        cwd=wt_path,
        check=False,
    )
    upstream = upstream_proc.stdout.strip() if upstream_proc.returncode == 0 else None
    compare_ref: str | None = upstream

    if not compare_ref and not is_main and base_branch and base_branch != branch:
        verify_base = _run_git(["rev-parse", "--verify", base_branch], cwd=wt_path, check=False)
        if verify_base.returncode == 0:
            compare_ref = base_branch

    if not compare_ref:
        return 0, 0, upstream, None

    rev_proc = _run_git(
        ["rev-list", "--left-right", "--count", f"HEAD...{compare_ref}"],
        cwd=wt_path,
        check=False,
    )
    if rev_proc.returncode != 0 or not rev_proc.stdout.strip():
        return 0, 0, upstream, compare_ref

    pieces = rev_proc.stdout.strip().split()
    if len(pieces) != 2:
        return 0, 0, upstream, compare_ref
    try:
        return int(pieces[0]), int(pieces[1]), upstream, compare_ref
    except ValueError:
        return 0, 0, upstream, compare_ref


def _check_merged_into_base(
    repo_root: Path,
    head_sha: str | None,
    base_branch: str | None,
    *,
    is_main: bool,
    bare: bool,
) -> bool:
    if is_main or bare or not head_sha or not base_branch:
        return False
    verify_base = _run_git(["rev-parse", "--verify", base_branch], cwd=repo_root, check=False)
    if verify_base.returncode != 0:
        return False
    proc = _run_git(
        ["merge-base", "--is-ancestor", head_sha, base_branch],
        cwd=repo_root,
        check=False,
    )
    return proc.returncode == 0


def _detect_agent_artifacts(wt_path: Path) -> tuple[list[str], list[str]]:
    """Detect coding-agent workspace artifacts in a worktree directory."""
    if not wt_path.is_dir():
        return [], []
    artifacts: list[str] = []
    agents: list[str] = []
    for artifact_name, agent_name in AGENT_ARTIFACT_MAP.items():
        if (wt_path / artifact_name).exists():
            artifacts.append(artifact_name)
            if agent_name not in agents:
                agents.append(agent_name)
    return sorted(artifacts), sorted(agents)


def _slugify_candidates(wt_path: Path) -> list[str]:
    """Generate candidate Claude Code ~/.claude/projects/<slug> directory names."""
    raw_variants = {str(wt_path), str(wt_path.resolve()), wt_path.as_posix(), wt_path.resolve().as_posix()}
    slugs: list[str] = []
    for variant in sorted(raw_variants):
        for pattern in (_SLUG_STRICT_RE, _SLUG_KEEP_UNDERSCORE_RE):
            slug = pattern.sub("-", variant)
            if slug and slug not in slugs:
                slugs.append(slug)
    return slugs


def discover_claude_sessions(
    wt_path: Path,
    claude_projects_dir: Path | None,
) -> ClaudeSessionInfo | None:
    """Locate relocated Claude Code transcripts for a worktree cwd without reading secrets."""
    projects_root = claude_projects_dir if claude_projects_dir is not None else (Path.home() / ".claude" / "projects")
    if not projects_root.is_dir():
        return None

    for slug in _slugify_candidates(wt_path):
        candidate_dir = projects_root / slug
        if not candidate_dir.is_dir():
            continue
        jsonl_files = sorted(
            (p for p in candidate_dir.iterdir() if p.is_file() and p.suffix == ".jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not jsonl_files:
            continue
        latest_mtime = dt.datetime.fromtimestamp(
            jsonl_files[0].stat().st_mtime, tz=dt.timezone.utc
        ).replace(microsecond=0)
        return ClaudeSessionInfo(
            project_dir=str(candidate_dir),
            session_count=len(jsonl_files),
            latest_session_at=latest_mtime.isoformat(),
            session_ids=[p.stem for p in jsonl_files[:20]],
        )
    return None


def _detect_base_branch(repo_root: Path, stanzas: list[dict[str, Any]], override: str | None) -> str | None:
    if override:
        return override
    if stanzas and not stanzas[0]["bare"] and stanzas[0]["branch"]:
        return str(stanzas[0]["branch"])
    for candidate in ("main", "master"):
        proc = _run_git(["rev-parse", "--verify", candidate], cwd=repo_root, check=False)
        if proc.returncode == 0:
            return candidate
    return None


def inspect_repo_worktrees(
    target: Path,
    *,
    claude_projects_dir: Path | None = None,
    base_branch_override: str | None = None,
) -> RepoSummary:
    """Inspect all worktrees belonging to the git repository at `target`."""
    probe_dir, common_dir = resolve_repo_context(target)
    porcelain_proc = _run_git(["worktree", "list", "--porcelain"], cwd=probe_dir, check=True)
    stanzas = parse_worktree_porcelain(porcelain_proc.stdout)

    if stanzas:
        main_root = stanzas[0]["path"]
        if stanzas[0]["bare"] and len(stanzas) > 1:
            repo_name = common_dir.parent.name if common_dir.name == ".git" else common_dir.name
        else:
            repo_name = main_root.name
    else:
        main_root = probe_dir
        repo_name = main_root.name

    base_branch = _detect_base_branch(main_root if main_root.is_dir() else probe_dir, stanzas, base_branch_override)
    allocations = _load_allocations(common_dir)

    worktrees: list[WorktreeInfo] = []
    for idx, entry in enumerate(stanzas):
        wt_path: Path = entry["path"]
        is_main = idx == 0 and not entry["bare"]
        exists = wt_path.is_dir()
        branch = (
            "(bare)"
            if entry["bare"]
            else "(detached)"
            if entry["detached"]
            else (entry["branch"] or "(unborn)")
        )
        head_sha: str | None = entry["head"]
        head_short = head_sha[:7] if head_sha else None

        if exists and not entry["bare"]:
            dirty_count, staged_count, unstaged_count, untracked_count = _inspect_dirty_counts(wt_path)
            ahead, behind, upstream, compare_ref = _inspect_ahead_behind(
                wt_path,
                branch,
                base_branch,
                is_main=is_main,
                has_head=bool(head_sha),
            )
            last_commit_at, last_commit_subject = _inspect_last_commit(wt_path)
            artifacts, agents = _detect_agent_artifacts(wt_path)
            claude_sessions = discover_claude_sessions(wt_path, claude_projects_dir)
            if claude_sessions and "claude" not in agents:
                agents = sorted([*agents, "claude"])
        else:
            dirty_count = staged_count = unstaged_count = untracked_count = 0
            ahead = behind = 0
            upstream = compare_ref = None
            last_commit_at = last_commit_subject = None
            artifacts, agents = [], []
            claude_sessions = None

        alloc_key = str(wt_path.resolve())
        alloc = allocations.get(alloc_key, {})
        dev_port = alloc.get("port") if isinstance(alloc.get("port"), int) else None
        alloc_agent = alloc.get("agent") if isinstance(alloc.get("agent"), str) else None
        if alloc_agent and alloc_agent not in agents:
            agents = sorted([*agents, alloc_agent])

        merged_into_base = _check_merged_into_base(
            main_root if main_root.is_dir() else probe_dir,
            head_sha,
            base_branch,
            is_main=is_main,
            bare=entry["bare"],
        )

        worktrees.append(
            WorktreeInfo(
                repo_name=repo_name,
                repo_root=str(main_root),
                path=str(wt_path),
                is_main=is_main,
                exists=exists,
                head=head_sha,
                head_short=head_short,
                branch=branch,
                detached=bool(entry["detached"]),
                bare=bool(entry["bare"]),
                locked=bool(entry["locked"]),
                lock_reason=str(entry["lock_reason"]),
                prunable=bool(entry["prunable"]),
                prune_reason=str(entry["prune_reason"]),
                dirty_count=dirty_count,
                staged_count=staged_count,
                unstaged_count=unstaged_count,
                untracked_count=untracked_count,
                ahead=ahead,
                behind=behind,
                upstream=upstream,
                ahead_behind_ref=compare_ref,
                merged_into_base=merged_into_base,
                last_commit_at=last_commit_at,
                last_commit_subject=last_commit_subject,
                agent_artifacts=artifacts,
                agents=agents,
                dev_port=dev_port,
                allocated_agent=alloc_agent,
                claude_sessions=claude_sessions,
            )
        )

    return RepoSummary(
        repo_name=repo_name,
        repo_root=str(main_root),
        git_common_dir=str(common_dir),
        base_branch=base_branch,
        worktrees=worktrees,
    )


def scan_workspace(
    root: Path,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    claude_projects_dir: Path | None = None,
    agent_filter: str | None = None,
    dirty_only: bool = False,
) -> ScanReport:
    """Scan a workspace directory for all repositories and linked worktrees."""
    repo_pairs = discover_git_repos(root, max_depth=max_depth)
    repos: list[RepoSummary] = []
    all_worktrees: list[WorktreeInfo] = []

    norm_agent = agent_filter.strip().lower() if agent_filter else None

    for probe_dir, _common_dir in repo_pairs:
        summary = inspect_repo_worktrees(probe_dir, claude_projects_dir=claude_projects_dir)
        filtered = summary.worktrees
        if norm_agent:
            filtered = [wt for wt in filtered if norm_agent in wt.agents]
        if dirty_only:
            filtered = [wt for wt in filtered if wt.dirty_count > 0]
        if filtered:
            summary.worktrees = filtered
            repos.append(summary)
            all_worktrees.extend(filtered)

    return ScanReport(
        schema_version=SCHEMA_VERSION,
        generated_at=_utc_now_iso(),
        root=str(root.resolve()),
        repos=repos,
        worktrees=all_worktrees,
    )


def _sanitize_branch_slug(branch: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch.strip()).strip("-.")
    return slug or "worktree"


def _find_env_files(source_root: Path) -> tuple[bool, list[str]]:
    """Inspect source repo root for .env.example vs raw secret .env* files."""
    has_example = (source_root / ".env.example").is_file()
    ignored_raw: list[str] = []
    if source_root.is_dir():
        for entry in sorted(source_root.iterdir(), key=lambda p: p.name):
            if not entry.is_file():
                continue
            if entry.name == ".env" or (entry.name.startswith(".env.") and entry.name != ".env.example"):
                ignored_raw.append(entry.name)
    return has_example, ignored_raw


def create_worktree(
    repo_path: Path,
    branch: str,
    *,
    target_path: Path | None = None,
    worktrees_dir: Path | None = None,
    base_ref: str | None = None,
    agent: str | None = None,
    copy_env_example: bool = False,
    explicit_port: int | None = None,
    port_base: int = DEFAULT_PORT_BASE,
    port_span: int = DEFAULT_PORT_SPAN,
    probe_socket: bool = True,
) -> CreateResult:
    """Safely create an isolated git worktree, branch, port allocation, and optional .env.example."""
    clean_branch = branch.strip()
    if not clean_branch:
        raise WorktreeHubError("Branch name cannot be empty", exit_code=2)

    ref_check = _run_git(["check-ref-format", "--branch", clean_branch], cwd=repo_path.resolve(), check=False)
    if ref_check.returncode != 0:
        raise WorktreeHubError(f"Invalid git branch name: {branch}", exit_code=2)

    summary = inspect_repo_worktrees(repo_path)
    repo_root = Path(summary.repo_root)
    common_dir = Path(summary.git_common_dir)

    for wt in summary.worktrees:
        if wt.branch == clean_branch and wt.exists:
            raise WorktreeHubError(
                f"Branch '{clean_branch}' is already checked out at {wt.path}",
                exit_code=1,
            )

    if target_path is not None:
        dest_path = target_path.resolve()
    else:
        slug = _sanitize_branch_slug(clean_branch)
        parent_dir = worktrees_dir.resolve() if worktrees_dir is not None else repo_root.parent
        dest_path = (parent_dir / f"{repo_root.name}-wt-{slug}").resolve()

    if dest_path.exists() and any(dest_path.iterdir()):
        raise WorktreeHubError(f"Target directory already exists and is non-empty: {dest_path}", exit_code=1)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Check whether the branch already exists locally.
    branch_exists_proc = _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{clean_branch}"],
        cwd=repo_root,
        check=False,
    )
    branch_exists = branch_exists_proc.returncode == 0

    if branch_exists and base_ref:
        raise WorktreeHubError(
            f"Branch '{clean_branch}' already exists locally; omit --base or choose a new branch name.",
            exit_code=1,
        )

    # Ensure repository has at least one commit before creating a linked worktree.
    head_proc = _run_git(["rev-parse", "--verify", "HEAD"], cwd=repo_root, check=False)
    if head_proc.returncode != 0:
        raise WorktreeHubError(
            "Repository has no commits yet (unborn HEAD); create an initial commit first.",
            exit_code=1,
        )

    allocations = _load_allocations(common_dir)
    live_paths = {str(Path(wt.path).resolve()) for wt in summary.worktrees if wt.exists}
    pruned_allocations = {k: v for k, v in allocations.items() if k in live_paths}
    used_ports = {
        v["port"] for v in pruned_allocations.values() if isinstance(v.get("port"), int)
    }

    assigned_port = assign_deterministic_port(
        repo_root,
        clean_branch,
        used_ports,
        port_base=port_base,
        port_span=port_span,
        explicit_port=explicit_port,
        probe_socket=probe_socket,
    )

    if branch_exists:
        add_args = ["worktree", "add", str(dest_path), clean_branch]
    else:
        add_args = ["worktree", "add", "-b", clean_branch, str(dest_path)]
        if base_ref:
            add_args.append(base_ref)

    _run_git(add_args, cwd=repo_root, check=True)

    # Record deterministic port and optional agent hint inside .git common dir (keeps worktree clean).
    normalized_agent = agent.strip().lower() if agent else None
    pruned_allocations[str(dest_path.resolve())] = {
        "branch": clean_branch,
        "port": assigned_port,
        "agent": normalized_agent,
        "created_at": _utc_now_iso(),
    }
    _save_allocations(common_dir, pruned_allocations)

    # Optional .env.example template copy — NEVER copy raw .env secret files.
    has_example, ignored_raw = _find_env_files(repo_root)
    env_copied = False
    if not copy_env_example:
        env_status = "skipped (not requested)"
    elif not has_example:
        env_status = "skipped (no .env.example in source repo)"
    else:
        src_example = repo_root / ".env.example"
        dst_example = dest_path / ".env.example"
        if dst_example.is_file():
            env_status = "already present in checked-out worktree"
        else:
            shutil.copy2(src_example, dst_example)
            env_copied = True
            env_status = "copied .env.example safely (raw .env files excluded)"

    return CreateResult(
        repo_root=str(repo_root),
        worktree_path=str(dest_path),
        branch=clean_branch,
        base_ref=base_ref or summary.base_branch,
        dev_port=assigned_port,
        agent=normalized_agent,
        env_example_copied=env_copied,
        env_example_status=env_status,
        ignored_env_files=ignored_raw,
    )


def _age_hours_from_iso(iso_ts: str | None, now: dt.datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        parsed = dt.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (now - parsed.astimezone(dt.timezone.utc)).total_seconds() / 3600.0)
    except ValueError:
        return None


def clean_worktrees(
    target: Path,
    *,
    base_branch_override: str | None = None,
    dry_run: bool = True,
    force: bool = False,
    delete_branch: bool = False,
    min_age_hours: float = 0.0,
) -> CleanReport:
    """Preview (--dry-run default) or remove stale merged/prunable worktrees safely."""
    summary = inspect_repo_worktrees(target, base_branch_override=base_branch_override)
    repo_root = Path(summary.repo_root)
    common_dir = Path(summary.git_common_dir)
    base_branch = summary.base_branch
    now = dt.datetime.now(dt.timezone.utc)

    candidates: list[CleanCandidate] = []
    skipped: list[CleanSkipped] = []
    errors: list[str] = []

    for wt in summary.worktrees:
        if wt.is_main:
            skipped.append(CleanSkipped(wt.path, wt.branch, "main worktree is protected"))
            continue
        if wt.bare:
            skipped.append(CleanSkipped(wt.path, wt.branch, "bare repository is protected"))
            continue
        if wt.locked:
            reason = f"locked ({wt.lock_reason})" if wt.lock_reason else "locked"
            skipped.append(CleanSkipped(wt.path, wt.branch, reason))
            continue

        if wt.prunable or not wt.exists:
            candidates.append(
                CleanCandidate(
                    repo_name=summary.repo_name,
                    path=wt.path,
                    branch=wt.branch,
                    reason="missing on disk (prunable worktree metadata)",
                    prunable=True,
                    merged_into_base=wt.merged_into_base,
                    dirty_count=0,
                )
            )
            continue

        if wt.dirty_count > 0 and not force:
            skipped.append(
                CleanSkipped(
                    wt.path,
                    wt.branch,
                    f"dirty ({wt.dirty_count} uncommitted file(s)); use --force to override",
                )
            )
            continue

        if not wt.merged_into_base:
            target_label = base_branch or "base branch"
            skipped.append(
                CleanSkipped(wt.path, wt.branch, f"unmerged into {target_label}")
            )
            continue

        if min_age_hours > 0:
            age_h = _age_hours_from_iso(wt.last_commit_at, now)
            if age_h is not None and age_h < min_age_hours:
                skipped.append(
                    CleanSkipped(
                        wt.path,
                        wt.branch,
                        f"last commit {age_h:.1f}h ago (< --min-age-hours {min_age_hours:g})",
                    )
                )
                continue

        reason = f"merged into {base_branch}" + (" (forced dirty)" if wt.dirty_count > 0 else "")
        candidates.append(
            CleanCandidate(
                repo_name=summary.repo_name,
                path=wt.path,
                branch=wt.branch,
                reason=reason,
                prunable=False,
                merged_into_base=True,
                dirty_count=wt.dirty_count,
            )
        )

    if not dry_run and candidates:
        allocations = _load_allocations(common_dir)
        need_prune = False
        alloc_changed = False

        for item in candidates:
            if item.prunable:
                need_prune = True
                item.removed = True
            else:
                rm_args = ["worktree", "remove"]
                if force:
                    rm_args.append("--force")
                rm_args.append(item.path)
                proc = _run_git(rm_args, cwd=repo_root, check=False)
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "unknown error").strip()
                    errors.append(f"Failed to remove {item.path}: {detail}")
                    continue
                item.removed = True

            norm_key = str(Path(item.path).resolve())
            if norm_key in allocations:
                allocations.pop(norm_key, None)
                alloc_changed = True

            if delete_branch and item.branch not in ("(detached)", "(bare)", "(unborn)"):
                del_proc = _run_git(["branch", "-d", item.branch], cwd=repo_root, check=False)
                if del_proc.returncode == 0:
                    item.branch_deleted = True

        if need_prune:
            prune_proc = _run_git(["worktree", "prune"], cwd=repo_root, check=False)
            if prune_proc.returncode != 0:
                errors.append(f"git worktree prune failed: {prune_proc.stderr.strip()}")
        if alloc_changed:
            _save_allocations(common_dir, allocations)

    return CleanReport(
        repo_root=str(repo_root),
        base_branch=base_branch,
        dry_run=dry_run,
        candidates=candidates,
        skipped=skipped,
        errors=errors,
    )


def _folder_display_name(wt: WorktreeInfo) -> str:
    agent_tag = f" ({', '.join(wt.agents)})" if wt.agents else ""
    port_tag = f" :{wt.dev_port}" if wt.dev_port else ""
    return f"{wt.repo_name} [{wt.branch}]{agent_tag}{port_tag}"


def export_vscode_manifest(
    root: Path,
    *,
    fmt: str = "json",
    max_depth: int = DEFAULT_MAX_DEPTH,
    agent_filter: str | None = None,
    claude_projects_dir: Path | None = None,
) -> str:
    """Generate a unified JSON, Markdown, or .code-workspace manifest for parallel sessions."""
    report = scan_workspace(
        root,
        max_depth=max_depth,
        claude_projects_dir=claude_projects_dir,
        agent_filter=agent_filter,
    )
    norm_fmt = fmt.strip().lower()
    folders = [{"name": _folder_display_name(wt), "path": wt.path} for wt in report.worktrees if wt.exists]

    if norm_fmt == "workspace":
        workspace_doc = {
            "folders": folders,
            "settings": {
                "worktreeHub.generatedAt": report.generated_at,
                "worktreeHub.sourceRoot": report.root,
                "git.detectSubmodules": False,
            },
        }
        return json.dumps(workspace_doc, indent=2, ensure_ascii=False) + "\n"

    if norm_fmt == "json":
        base_dict = report.to_dict()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": report.generated_at,
            "root": report.root,
            "summary": base_dict["summary"],
            "vscode_folders": folders,
            "sessions": [wt.to_dict() for wt in report.worktrees],
        }
        return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"

    if norm_fmt in ("markdown", "md"):
        summary = report.to_dict()["summary"]
        lines = [
            "# Multi-Agent Git Worktree Session Manifest",
            "",
            f"- **Generated**: `{report.generated_at}`",
            f"- **Root**: `{report.root}`",
            f"- **Repositories**: {summary['total_repos']} | **Worktrees**: {summary['total_worktrees']} "
            f"| **Dirty**: {summary['dirty_worktrees']} | **Relocated Claude Sessions**: {summary['claude_relocated_sessions']}",
            "",
        ]
        if not report.worktrees:
            lines.append("_No git worktrees discovered under this root._")
            return "\n".join(lines) + "\n"

        lines.extend(
            [
                "| Repo | Branch | Agents | Dirty | Ahead/Behind | Port | Claude Transcripts | Last Commit | Path |",
                "| --- | --- | --- | ---: | --- | ---: | ---: | --- | --- |",
            ]
        )
        for wt in report.worktrees:
            agents_str = ", ".join(wt.agents) if wt.agents else "-"
            ab_str = f"+{wt.ahead}/-{wt.behind}"
            if wt.ahead_behind_ref:
                ab_str += f" ({wt.ahead_behind_ref})"
            port_str = str(wt.dev_port) if wt.dev_port else "-"
            claude_cnt = str(wt.claude_sessions.session_count) if wt.claude_sessions else "0"
            commit_str = wt.last_commit_at[:19] if wt.last_commit_at else "-"
            lines.append(
                f"| {wt.repo_name} | `{wt.branch}` | {agents_str} | {wt.dirty_count} | "
                f"{ab_str} | {port_str} | {claude_cnt} | {commit_str} | `{wt.path}` |"
            )

        relocated = [wt for wt in report.worktrees if wt.claude_sessions]
        if relocated:
            lines.extend(["", "## Relocated Claude Code Transcript Directories", ""])
            for wt in relocated:
                cs = wt.claude_sessions
                assert cs is not None
                lines.append(
                    f"- `{wt.branch}` (`{wt.path}`): **{cs.session_count}** session(s) in `{cs.project_dir}`"
                    + (f" (latest: `{cs.latest_session_at}`)" if cs.latest_session_at else "")
                )

        return "\n".join(lines) + "\n"

    raise WorktreeHubError(f"Unsupported export format: {fmt}", exit_code=2)


def _format_worktree_table(worktrees: Sequence[WorktreeInfo]) -> str:
    if not worktrees:
        return "No git worktrees found."

    headers = ("REPO", "BRANCH", "AGENTS", "DIRTY", "AHEAD/BEHIND", "PORT", "CLAUDE_SESS", "LAST_COMMIT", "PATH")
    rows: list[tuple[str, ...]] = []
    for wt in worktrees:
        agents_str = ",".join(wt.agents) if wt.agents else "-"
        ab_str = f"+{wt.ahead}/-{wt.behind}"
        port_str = str(wt.dev_port) if wt.dev_port else "-"
        claude_cnt = str(wt.claude_sessions.session_count) if wt.claude_sessions else "0"
        commit_str = wt.last_commit_at[:19] if wt.last_commit_at else "-"
        branch_label = wt.branch + (" *" if wt.is_main else "") + (" [locked]" if wt.locked else "")
        rows.append(
            (
                wt.repo_name,
                branch_label,
                agents_str,
                str(wt.dirty_count),
                ab_str,
                port_str,
                claude_cnt,
                commit_str,
                wt.path,
            )
        )

    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = [
        "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))),
        "  ".join("-" * widths[i] for i in range(len(headers))),
    ]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def _cmd_scan(args: argparse.Namespace) -> int:
    claude_dir = Path(args.claude_projects_dir) if args.claude_projects_dir else None
    report = scan_workspace(
        Path(args.root),
        max_depth=args.max_depth,
        claude_projects_dir=claude_dir,
        agent_filter=args.agent,
        dirty_only=args.dirty_only,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 0

    data = report.to_dict()["summary"]
    print(
        f"Scanned {report.root} | repos={data['total_repos']} worktrees={data['total_worktrees']} "
        f"dirty={data['dirty_worktrees']} claude_relocated={data['claude_relocated_sessions']}"
    )
    print(_format_worktree_table(report.worktrees))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    claude_dir = Path(args.claude_projects_dir) if args.claude_projects_dir else None
    summary = inspect_repo_worktrees(
        Path(args.target),
        claude_projects_dir=claude_dir,
        base_branch_override=args.base,
    )
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
        return 0

    print(
        f"Repository: {summary.repo_name} ({summary.repo_root}) | base={summary.base_branch or '-'} "
        f"| worktrees={len(summary.worktrees)}"
    )
    print(_format_worktree_table(summary.worktrees))
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    result = create_worktree(
        Path(args.repo),
        args.branch,
        target_path=Path(args.path) if args.path else None,
        worktrees_dir=Path(args.worktrees_dir) if args.worktrees_dir else None,
        base_ref=args.base,
        agent=args.agent,
        copy_env_example=args.copy_env_example,
        explicit_port=args.port,
        port_base=args.port_base,
        port_span=args.port_span,
        probe_socket=not args.no_port_probe,
    )
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0

    print(f"Created worktree: {result.worktree_path}")
    print(f"  Branch       : {result.branch} (base: {result.base_ref or '-'})")
    print(f"  Dev Port     : {result.dev_port}")
    if result.agent:
        print(f"  Agent Tag    : {result.agent}")
    print(f"  .env.example : {result.env_example_status}")
    if result.ignored_env_files:
        print(f"  Safety Guard : raw secret file(s) NEVER copied: {', '.join(result.ignored_env_files)}")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    dry_run = not args.apply
    report = clean_worktrees(
        Path(args.target),
        base_branch_override=args.base,
        dry_run=dry_run,
        force=args.force,
        delete_branch=args.delete_branch,
        min_age_hours=args.min_age_hours,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        return 1 if report.errors else 0

    mode_label = "DRY-RUN PREVIEW (pass --apply to remove)" if report.dry_run else "APPLIED CLEANUP"
    print(f"[{mode_label}] repo={report.repo_root} base={report.base_branch or '-'}")
    if not report.candidates:
        print("  No stale merged or prunable worktrees to remove.")
    else:
        for item in report.candidates:
            verb = "REMOVED" if item.removed else "WOULD REMOVE"
            print(f"  - {verb}: {item.path} [{item.branch}] ({item.reason})")
    if report.skipped:
        print("  Preserved:")
        for skip in report.skipped:
            print(f"    * {skip.path} [{skip.branch}]: {skip.reason}")
    for err in report.errors:
        print(f"  ERROR: {err}", file=sys.stderr)
    return 1 if report.errors else 0


def _cmd_export_vscode(args: argparse.Namespace) -> int:
    claude_dir = Path(args.claude_projects_dir) if args.claude_projects_dir else None
    rendered = export_vscode_manifest(
        Path(args.root),
        fmt=args.format,
        max_depth=args.max_depth,
        agent_filter=args.agent,
        claude_projects_dir=claude_dir,
    )
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.format} manifest to {out_path}")
    else:
        sys.stdout.write(rendered)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worktree-hub",
        description=(
            "Zero-dependency multi-agent git worktree hub for Claude Code, Codex, "
            "Antigravity, Cursor, and VS Code."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan
    p_scan = subparsers.add_parser("scan", help="Discover all git worktrees and coding-agent artifacts.")
    p_scan.add_argument("root", nargs="?", default=".", help="Workspace or repo root to scan (default: .)")
    p_scan.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="Max directory recursion depth.")
    p_scan.add_argument("--agent", help="Filter worktrees by detected agent (claude, codex, gemini, cursor, agent).")
    p_scan.add_argument("--dirty-only", action="store_true", help="Only include worktrees with uncommitted changes.")
    p_scan.add_argument("--claude-projects-dir", help="Override ~/.claude/projects directory path.")
    p_scan.add_argument("--json", action="store_true", help="Output structured JSON.")
    p_scan.set_defaults(func=_cmd_scan)

    # status
    p_status = subparsers.add_parser("status", help="Inspect worktree status for a single git repository.")
    p_status.add_argument("target", nargs="?", default=".", help="Repository or worktree path (default: .)")
    p_status.add_argument("--base", help="Base branch override for ahead/behind and merge checks.")
    p_status.add_argument("--claude-projects-dir", help="Override ~/.claude/projects directory path.")
    p_status.add_argument("--json", action="store_true", help="Output structured JSON.")
    p_status.set_defaults(func=_cmd_status)

    # create
    p_create = subparsers.add_parser(
        "create",
        help="Safely create an isolated worktree + branch + deterministic port + optional .env.example.",
    )
    p_create.add_argument("branch", help="Branch name to create or attach.")
    p_create.add_argument("--repo", default=".", help="Target repository path (default: .)")
    p_create.add_argument("--path", help="Explicit destination path for the new worktree.")
    p_create.add_argument("--worktrees-dir", help="Parent directory for auto-named worktree folders.")
    p_create.add_argument("--base", help="Base branch/commit when creating a new branch.")
    p_create.add_argument("--agent", help="Optional agent tag to record (e.g. claude, codex, cursor, gemini).")
    p_create.add_argument(
        "--copy-env-example",
        action="store_true",
        help="Copy .env.example if untracked in source repo (NEVER copies raw .env secrets).",
    )
    p_create.add_argument("--port", type=int, help="Explicit dev port instead of deterministic hash assignment.")
    p_create.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE, help="Start of dev port pool.")
    p_create.add_argument("--port-span", type=int, default=DEFAULT_PORT_SPAN, help="Size of dev port pool.")
    p_create.add_argument("--no-port-probe", action="store_true", help="Skip localhost socket bind check.")
    p_create.add_argument("--json", action="store_true", help="Output structured JSON.")
    p_create.set_defaults(func=_cmd_create)

    # clean
    p_clean = subparsers.add_parser(
        "clean",
        help="Safely preview (--dry-run default) or remove stale merged worktrees.",
    )
    p_clean.add_argument("target", nargs="?", default=".", help="Repository or worktree path (default: .)")
    p_clean.add_argument("--base", help="Base branch to check merge status against.")
    mode_group = p_clean.add_mutually_exclusive_group()
    mode_group.add_argument("--dry-run", action="store_true", help="Preview removable worktrees (default).")
    mode_group.add_argument("--apply", "--execute", dest="apply", action="store_true", help="Actually remove eligible worktrees.")
    p_clean.add_argument("--force", action="store_true", help="Allow removing dirty merged worktrees with --apply.")
    p_clean.add_argument("--delete-branch", action="store_true", help="Also delete merged branch refs with git branch -d.")
    p_clean.add_argument("--min-age-hours", type=float, default=0.0, help="Only clean worktrees older than N hours.")
    p_clean.add_argument("--json", action="store_true", help="Output structured JSON.")
    p_clean.set_defaults(func=_cmd_clean)

    # export-vscode
    p_export = subparsers.add_parser(
        "export-vscode",
        help="Export JSON, Markdown, or .code-workspace manifest of all parallel worktree sessions.",
    )
    p_export.add_argument("root", nargs="?", default=".", help="Workspace or repo root to export (default: .)")
    p_export.add_argument(
        "--format",
        choices=("json", "markdown", "md", "workspace"),
        default="json",
        help="Output format (default: json).",
    )
    p_export.add_argument("--output", "-o", help="Write manifest to file instead of stdout.")
    p_export.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="Max directory recursion depth.")
    p_export.add_argument("--agent", help="Filter exported sessions by detected agent.")
    p_export.add_argument("--claude-projects-dir", help="Override ~/.claude/projects directory path.")
    p_export.set_defaults(func=_cmd_export_vscode)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except WorktreeHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
