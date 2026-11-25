# worktree-hub

Small, zero-dependency Python 3.11+ CLI for managing parallel `git worktree` checkouts when you run Claude Code, Codex, Antigravity, Cursor, and VS Code side by side.

Built after hitting the issue described in [anthropics/claude-code#81024](https://github.com/anthropics/claude-code/issues/81024). When a session moves into a linked worktree, Claude Code writes its transcript under `~/.claude/projects/<slugified-cwd>/`. The VS Code extension only reads the folder currently open in that window, so you end up opening one window per worktree just to find old sessions, while dev servers fight over the same localhost ports.

## What it does

- `scan`: Walks a workspace root, groups repos by `git-common-dir` so sibling worktrees are not counted twice, and reports branch, dirty counts (staged/unstaged/untracked), ahead/behind counts, last commit timestamp, local agent directories (`.claude`, `.codex`, `.gemini`, `.cursor`, `.agent`), and relocated `.jsonl` session counts under `~/.claude/projects/<slugified-cwd>/`.
- `status`: Prints a single-repo summary including `merged_into_base`, allocated dev port, and detected agents.
- `create`: Creates a worktree and branch, picks a deterministic localhost port from `3100-3999` (configurable with `--port-base` / `--port-span`), and optionally copies `.env.example` when `--copy-env-example` is passed. Raw `.env` and `.env.*` files are never copied.
- `clean`: Previews stale merged or missing (prunable) worktrees with `--dry-run` enabled by default. Passing `--apply` removes eligible worktrees while still skipping the main worktree, bare repos, locked worktrees, unmerged branches, and dirty worktrees (unless `--force` is also set).
- `export-vscode`: Writes a consolidated `json` manifest, `markdown` table, or `.code-workspace` file (`--format workspace`) so all live worktrees can be opened in one VS Code window.

## How port allocation works

When `create` sets up a worktree, it hashes `<repo_root>::<branch>` with SHA-256 to pick a stable starting port in the configured range, then checks:

1. Existing entries in `<git-common-dir>/worktree-hub/allocations.json` (pruning worktree paths that no longer exist on disk first).
2. Whether `127.0.0.1:<port>` can be bound via a plain TCP socket check (skip with `--no-port-probe`).

Allocations are saved in `.git/worktree-hub/allocations.json` rather than inside the working tree, so a newly created worktree starts with zero untracked files in `git status`.

## Install or run directly

Requires Python 3.11+ and `git`. No third-party packages are used.

```bash
# Run directly
python3 worktree_hub.py --help

# Or install locally
pip install .
worktree-hub --help
```

## Usage

```bash
# Scan a workspace directory (or filter by agent / dirty state)
worktree-hub scan ~/workspace
worktree-hub scan ~/workspace --agent claude --json
worktree-hub scan ~/workspace --dirty-only

# Create a worktree with a deterministic port and .env.example copy
worktree-hub create feat/oauth-fix --repo ~/workspace/app --agent claude --copy-env-example

# Check one repository
worktree-hub status ~/workspace/app

# Preview stale merged worktrees (dry-run by default)
worktree-hub clean ~/workspace/app

# Remove clean merged worktrees and delete their local branches
worktree-hub clean ~/workspace/app --apply --delete-branch

# Export a single VS Code workspace or session manifest
worktree-hub export-vscode ~/workspace --format workspace --output multi-agent.code-workspace
worktree-hub export-vscode ~/workspace --format json --output sessions.json
worktree-hub export-vscode ~/workspace --format markdown --output WORKTREES.md
```

## Caveats and limits

- **Squash-merges**: `clean` checks merge status with `git merge-base --is-ancestor`. If a PR was squash-merged on GitHub, Git still treats the local branch commit as unmerged, so `clean` will skip it until you remove it manually or pass `--base` against a branch that contains that commit.
- **Per-repo port file**: `allocations.json` lives inside each repo's `.git/worktree-hub/` directory rather than a global cross-repo SQLite database. Cross-repo collisions are still avoided if the other server is running, because `create` probes `127.0.0.1` before assigning a port.
- **Claude transcript indexing**: `scan` and `export-vscode` only match directory slugs under `~/.claude/projects/` (or `--claude-projects-dir`) and count `.jsonl` files plus their modification timestamps. Transcript contents are never read or copied.
- **Windows vs POSIX slugs**: Directory slug matching normalizes non-alphanumeric characters to `-`, matching standard Claude Code paths, though custom symlinked workspace roots can produce a different slug if opened via the unresolved path.

## Tests

The test suite (`test_worktree_hub.py`) contains 8 `unittest` tests backed by temporary local git repositories:

```bash
python3 -m unittest discover -s . -v
```

## License

MIT. See [LICENSE](LICENSE).
