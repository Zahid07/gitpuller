# gitpuller

A lightweight utility to keep a git repository in sync with its remote, designed
to run **inside Mage AI pipelines** as an auto-pull step. It authenticates with an
SSH deploy key stored in an environment variable, forces the local checkout to
match the remote branch (even if someone manually edited files on the runner),
and sends a de-duplicated Slack alert if anything goes wrong.

---

## Why this exists

Mage runners are long-lived boxes. If anyone manually edits a tracked file,
adds a stray file, or commits locally, a plain `git pull` fails with an opaque
error like:

```
Command '['git', '-c', 'core.sshCommand=...', 'pull', 'git@github.com:...', 'master']'
returned non-zero exit status 1.
```

gitpuller solves three problems at once:

1. **Clear errors** — surfaces the actual git output, not the wrapper message.
2. **Self-healing sync** — discards local drift so the pull can't be blocked.
3. **Alerting without spam** — pings Slack on failure, but suppresses repeats of
   the same error within a configurable window.

---

## Installation

```bash
pip install salla_gitpuller
```

Dependency: [`requests`](https://pypi.org/project/requests/) (installed
automatically). Optional: `mage-ai` — only needed if you want alert-suppression
state to persist across pipeline runs (see [State management](#state-management)).

---

## Quick start

```python
from gitpuller import GitPullExecutor

executor = GitPullExecutor(
    use_mage_ai=True,  # persist alert state via Mage
    # Slack: set CDM_PROD_SLACK_BOT_TOKEN (threaded daily alerts).
    # Optional: CDM_PROD_DB_SLACK_CHANNEL (defaults to C05MLHR55JT).
)

result = executor.execute_with_alerting(
    repo_path="/home/src/my-repo",
    git_url="git@github.com:my-org/my-repo.git",
    workspace_name="myworkspace",   # reads the private key from {workspace_name}_SSHKEY
    # branch omitted -> defaults to "master"
)

print(result["git_pull_status"])     # "success"
print(result["discarded_changes"])   # what local drift (if any) was wiped
```

On failure, `execute_with_alerting` sends a Slack alert (subject to suppression)
as a **reply in today's daily thread**, then **re-raises**, so the Mage pipeline
still fails loudly.

---

## How it works

`execute_git_pull` does **not** run `git pull`. Instead it forces the local repo
to exactly match the remote, which is robust against manual edits *and* divergent
history (local commits / rewritten history) that a stash-based approach can't
handle:

1. **Prepare the SSH key** — the private deploy key is read from an env var,
   normalized (strips wrapping quotes, converts literal `\n` to real newlines,
   ensures a trailing newline), and written to `~/.ssh` with strict `0600`
   permissions. It is injected per-command via
   `git -c core.sshCommand="ssh -i <key> -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"`.
2. **`git fetch <url> <branch>`** — `FETCH_HEAD` now points at the remote tip.
3. **Snapshot + log local drift** — before anything is discarded, it records:
   - **working tree changes** — uncommitted edits and untracked files
     (`git status --porcelain`),
   - **local-only commits** — commits on the runner but not the remote
     (`FETCH_HEAD..HEAD`),
   and prints them to the pipeline log so you have a record of what was wiped.
4. **`git reset --hard FETCH_HEAD`** — makes the working tree and branch pointer
   match the remote exactly.
5. **`git clean -fd`** — removes untracked files/directories so the tree truly
   matches remote. **Ignored files are preserved** (no `-x`), so runner-local
   `.env` files and deploy keys survive.
6. **Cleanup** — the key file is removed and the working directory is restored,
   even on failure (`finally`).

Any failing step raises a `RuntimeError` containing the **real git stdout/stderr
and exit code**, which becomes the Slack alert body and the pipeline error.

> ⚠️ **This is destructive by design.** Local changes on the runner are treated as
> contamination and discarded. Don't point gitpuller at a repo where the runner
> holds work you intend to keep.

---

## API

### `GitPullExecutor(slack_webhook_url=None, slack_bot_token=None, slack_channel=None, use_mage_ai=False, state_manager=None)`

| Param | Description |
|-------|-------------|
| `slack_bot_token` | Slack bot token (`xoxb-...`). Falls back to `CDM_PROD_SLACK_BOT_TOKEN`. Preferred — enables daily-thread replies. |
| `slack_channel` | Channel ID/name. Falls back to `CDM_PROD_DB_SLACK_CHANNEL`, then `C05MLHR55JT`. |
| `slack_webhook_url` | Incoming-webhook URL. Falls back to `CDM_SLACK_WEBHOOK_URL`. Used only when no bot token is set (no threading). |
| `use_mage_ai` | If `True`, persist alert-suppression state via Mage global variables (falls back to in-memory if Mage isn't installed). |
| `state_manager` | Inject a custom `StateManager`; overrides `use_mage_ai`. |

If neither a bot token nor a webhook is configured, gitpuller still runs and
prints a warning instead of failing construction.

### Slack daily thread

When `CDM_PROD_SLACK_BOT_TOKEN` is set, failures are posted with `chat.postMessage`:

1. Open (or reuse) one parent message **per workspace per calendar day**:
   `🚨 Git Pull Failures {workspace_name} — YYYY-MM-DD`.
2. Post each later failure for that workspace as a **thread reply**.

Daily `thread_ts` values are stored in **one unified Redis hash** shared by every
Mage workspace (the key is **not** prefixed with `MAGE_WORKSPACE_NAME`):

```text
gitpuller:slack_thread:{channel}:{YYYY-MM-DD}
  cloud_data  → JSON { workspace, thread_ts, errors: [{workspace, repo, error, at, reply_ts}, ...] }
  partner     → JSON { workspace, thread_ts, errors: [...] }
TTL: until midnight (CDM_SLACK_THREAD_TZ, default UTC)
```

Each workspace keeps the Slack parent `thread_ts` plus up to 20 git errors
from that day (error text capped at 1500 chars). Legacy plain `thread_ts`
strings are still read.

Redis comes from Mage `io_config.yaml` (`REDIS_HOST` / `REDIS_PORT` /
`REDIS_PASSWORD`), or those same env vars. If Redis is unavailable, gitpuller
falls back to Slack `conversations.history`.

| Env var | Role |
|---------|------|
| `CDM_PROD_SLACK_BOT_TOKEN` | Required for threading. |
| `CDM_PROD_DB_SLACK_CHANNEL` | Channel; default `C05MLHR55JT`. |
| `CDM_SLACK_THREAD_TZ` | Timezone for the daily parent date; default `UTC`. |
| `CDM_PAUSE_SLACK_MESSAGES` | Set to `1` to skip Slack. |
| `CDM_SLACK_WEBHOOK_URL` | Legacy fallback when no bot token is set. |

The bot must be in the channel (`chat:write`). History reuse also needs
`channels:history` (or `groups:history` for a private channel).

### `execute_with_alerting(...)` → `dict`

Runs the sync and, on failure, alerts Slack (with suppression) then re-raises.

| Param | Default | Description |
|-------|---------|-------------|
| `repo_path` | — | Absolute path to the local repo (must exist). |
| `git_url` | — | SSH remote URL, e.g. `git@github.com:Org/repo.git`. |
| `branch` | `"master"` | Branch to sync to. **Note: defaults to `master`, not `main`.** |
| `ssh_key` | `None` | Private key material. If omitted, read from `{workspace_name}_SSHKEY`. |
| `workspace_name` | `None` | Used to locate the key env var and name the key file. |
| `pipeline_uuid` | `"auto_git_pull"` | Key under which alert state is stored. |
| `suppression_hours` | `1` | Don't re-alert on the *same* error within this many hours. |
| `key_filename` | `None` | Override the on-disk key filename. |
| `ssh_dir` | `"/home/src/.ssh"` | Directory to write the key into. |
| `webhook_url` | `None` | Legacy webhook override for this call. Ignored when a bot token is configured. |

### `execute_git_pull(...)` → `dict`

Same signature as above (minus `pipeline_uuid` / `suppression_hours`). Performs
the sync **without** alerting — use this if you handle errors yourself.

### Return value

```python
{
    "workspace": "myworkspace",
    "repo_path": "/home/src/my-repo",
    "git_pull_status": "success",          # or raises on error
    "git_pull_output": "HEAD is now at <sha> <subject>",
    "discarded_changes": {
        "working_tree_changes": "?? stray.txt",      # git status --porcelain output
        "local_commits": "949688e local-only commit" # FETCH_HEAD..HEAD output
    },
    "key_env_var_used": "myworkspace_SSHKEY",
}
```

---

## SSH key setup

Provide the **private** deploy key as an environment variable named
`{workspace_name}_SSHKEY` (e.g. `myworkspace_SSHKEY`), or pass `ssh_key=` directly.
The matching public key must be registered as a deploy key on the GitHub repo.

The key may be stored with literal `\n` (single-line) or real newlines — both are
handled. Wrapping quotes are stripped automatically.

### Use a read-only deploy key

gitpuller is a one-way mirror (remote → runner) and **never pushes**. Its only
remote operation is `git fetch`; the reset and clean steps are local. So the
deploy key only needs **read access** — leave GitHub's *"Allow write access"*
checkbox **unchecked**. This is the least-privilege setup and means the runner
can never push its discarded local changes back upstream.

Setup steps:

1. Generate a dedicated key pair: `ssh-keygen -t ed25519 -f deploy_key -N ""`.
2. On the GitHub repo: **Settings → Deploy keys → Add deploy key**, paste
   `deploy_key.pub`, and leave **Allow write access unchecked**.
3. Store the **private** key (`deploy_key`) in the `{workspace_name}_SSHKEY` env var.

**Note:** GitHub deploy keys are **per-repository** — each repo you sync needs its
own key pair and its own `{workspace_name}_SSHKEY` env var.

On first connection the remote host key is auto-accepted
(`StrictHostKeyChecking=accept-new`), i.e. trust-on-first-use rather than a
pre-pinned fingerprint.

---

## State management

Alert suppression needs to remember the last error and when it was alerted:

- **`InMemoryStateManager`** (default) — process-local; suppression only works
  within a single run.
- **`MageAIStateManager`** (`use_mage_ai=True`) — persists across runs via Mage
  global variables, so repeated failures across scheduled runs stay de-duplicated.
- **`StateManager`** — subclass it to plug in your own backend (e.g. Redis, a DB).

---

## Build & release

```bash
rm -rf build dist *.egg-info
python -m build
# then upload to PyPI (twine upload dist/*) and bump the version in pyproject.toml
```

Keep the version in sync in **both** `pyproject.toml` and `gitpuller/__init__.py`.

---

## Changelog

### 1.2.0 (current)

- **Threaded Slack alerts.** Failures post as replies under one daily parent
  **per workspace** (`🚨 Git Pull Failures {workspace} — YYYY-MM-DD`) via
  `CDM_PROD_SLACK_BOT_TOKEN` and `CDM_PROD_DB_SLACK_CHANNEL`
  (default `C05MLHR55JT`). Daily `thread_ts` lives in one unified Redis hash
  (`gitpuller:slack_thread:{channel}:{YYYY-MM-DD}`, field = workspace, TTL
  until midnight) shared across all Mage workspaces. Each workspace field is
  JSON: `thread_ts` plus that day's git errors.
- Incoming webhooks (`CDM_SLACK_WEBHOOK_URL` / `slack_webhook_url`) remain as a
  non-threaded fallback when no bot token is set.
- Missing Slack credentials no longer raise on `GitPullExecutor` construction;
  alerts are skipped with a warning.

### 1.1.0

Reliability and clarity overhaul.

- **Self-healing sync.** Replaced `git pull` with `git fetch` →
  `git reset --hard FETCH_HEAD` → `git clean -fd`. Manual edits, stray files, and
  even local commits / divergent history on the runner no longer break the sync.
  Ignored files (`.env`, keys) are preserved.
- **Clear error messages.** Failures now raise with the real git stdout/stderr and
  exit code instead of the opaque
  `Command '[...]' returned non-zero exit status 1.` wrapper. The same detail flows
  into the Slack alert.
- **Audit log of discarded changes.** Before resetting, the working-tree drift and
  any local-only commits are logged and returned under `discarded_changes`, so
  there's always a record of what was wiped.
- **Packaging fixes.** Declared the previously-missing `requests` dependency; synced
  the version between `pyproject.toml` and `__init__.py`.
- **Docs & comments.** Full README and inline documentation across all modules.

> **Migration note:** `git_pull_output` now reflects `reset --hard` output
> (`HEAD is now at <sha> <subject>`) rather than pull's `Updating x..y` /
> `Already up to date`. The result key `recovery_steps` (briefly present during
> development) is replaced by `discarded_changes`. Update any code that parses
> these. The public method signatures are unchanged.

### 1.0.x (previous)

- Initial release. Ran a plain `git pull <url> <branch>` over an SSH deploy key.
- Slack alerting with same-error suppression (`AlertManager` + `StateManager`,
  in-memory or Mage-backed).
- **Limitations addressed in 1.1.0:** any manual change on the runner caused the
  pull to fail; errors were opaque wrapper messages; `requests` was imported but
  not declared as a dependency.

---

<sub>Created and maintained by Mohammed Junaid and Muhammad Zahid.</sub>
