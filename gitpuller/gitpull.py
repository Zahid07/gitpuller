"""
Core git-pull executor.

Pulls a repository over SSH using a deploy key supplied via an environment
variable, and is resilient to manual local changes on the runner: it stashes
local edits before pulling and falls back to a hard reset if the pull still
fails. On unrecoverable failure it raises a clear, git-output-rich error and
(optionally) fires a Slack alert with suppression handled by ``AlertManager``.
"""

import os
import subprocess
from typing import Dict, Any, Optional
from datetime import datetime
from .alert_manager import AlertManager
from .slack_notifier import SlackNotifier


class GitPullExecutor:
    """Orchestrates the SSH-key setup, git pull, recovery, and alerting flow."""

    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        slack_bot_token: Optional[str] = None,
        slack_channel: Optional[str] = None,
        use_mage_ai: bool = False,
        state_manager: Optional[Any] = None
    ):
        # Slack sink for failure alerts (bot token + daily thread preferred).
        self.slack_notifier = SlackNotifier(
            webhook_url=slack_webhook_url,
            bot_token=slack_bot_token,
            channel=slack_channel,
        )
        # Decides whether/when to alert (de-duplicates repeated identical errors).
        self.alert_manager = AlertManager(state_manager=state_manager, use_mage_ai=use_mage_ai)

    def normalize_ssh_key(self, key_material: str) -> str:
        """
        Clean up a deploy key read from an env var so git/ssh will accept it.

        Env vars often arrive wrapped in quotes and/or with literal ``\\n``
        sequences instead of real newlines; ssh requires real newlines and a
        trailing newline on the final line.
        """
        if not key_material:
            return key_material

        # Remove any quotes that might be wrapping the key.
        key_material = key_material.strip().strip('"').strip("'")

        # Convert literal "\n" sequences into actual newlines.
        if "\\n" in key_material:
            key_material = key_material.replace("\\n", "\n")

        # ssh rejects keys whose final line lacks a trailing newline.
        if not key_material.endswith("\n"):
            key_material += "\n"

        return key_material

    def prepare_ssh_key(
        self,
        key_material: str,
        key_filename: str = "deploy_key",
        ssh_dir: str = "/home/src/.ssh"
    ) -> str:
        """
        Write the private deploy key to disk with the strict permissions ssh
        requires (0700 dir, 0600 file) and return the key path.
        """
        os.makedirs(ssh_dir, exist_ok=True)
        os.chmod(ssh_dir, 0o700)

        key_path = os.path.join(ssh_dir, key_filename)

        # Write the key, then lock it down — ssh refuses world-readable keys.
        with open(key_path, "w") as f:
            f.write(key_material)
        os.chmod(key_path, 0o600)

        return key_path
    
    def _run_git(
        self,
        args: list,
        ssh_command: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        """
        Run a git command and return the CompletedProcess (no exception on failure).

        When ``ssh_command`` is provided it is injected via ``-c core.sshCommand``
        so remote operations use the deploy key.
        """
        cmd = ["git"]
        if ssh_command:
            cmd += ["-c", f"core.sshCommand={ssh_command}"]
        cmd += args
        return subprocess.run(cmd, capture_output=True, text=True)

    @staticmethod
    def _format_git_error(label: str, result: subprocess.CompletedProcess) -> str:
        """Build a human-readable error string from a failed git command."""
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if not output:
            output = "(no output from git)"
        return f"{label} failed (exit code {result.returncode}):\n{output}"

    def _capture_local_changes(self) -> Dict[str, str]:
        """
        Snapshot the local deviations a hard reset is about to discard.

        Must be called *after* ``git fetch`` so ``FETCH_HEAD`` points at the
        remote tip. Captures two kinds of drift:

        * ``working_tree_changes`` — uncommitted edits and untracked files
          (``git status --porcelain``; ``??`` lines are untracked).
        * ``local_commits`` — commits that exist locally but not on the remote
          (``FETCH_HEAD..HEAD``), which the reset will throw away.
        """
        status = self._run_git(["status", "--porcelain"])
        local_commits = self._run_git(["log", "--oneline", "FETCH_HEAD..HEAD"])
        return {
            "working_tree_changes": (status.stdout or "").strip(),
            "local_commits": (local_commits.stdout or "").strip(),
        }

    @staticmethod
    def _log_discarded_changes(changes: Dict[str, str]) -> None:
        """Print, for the pipeline log, what is about to be discarded (if any)."""
        working_tree = changes.get("working_tree_changes", "")
        local_commits = changes.get("local_commits", "")

        if not working_tree and not local_commits:
            print("✅ No local changes detected; working tree already matches remote.")
            return

        print("⚠️ Local changes detected on the runner — these will be DISCARDED by reset:")
        if working_tree:
            print("  Uncommitted / untracked files:")
            for line in working_tree.splitlines():
                print(f"    {line}")
        if local_commits:
            print("  Local-only commits (not on remote):")
            for line in local_commits.splitlines():
                print(f"    {line}")

    def execute_git_pull(
        self,
        repo_path: str,
        git_url: str,
        branch: str = "master",
        ssh_key: Optional[str] = None,
        workspace_name: Optional[str] = None,
        key_filename: Optional[str] = None,
        ssh_dir: str = "/home/src/.ssh"
    ) -> Dict[str, Any]:
        """
        Force the local repo to exactly match the remote branch.

        Strategy (resilient to manual edits / divergent history on the runner):

        1. ``git fetch`` the target branch.
        2. Snapshot and log any local changes that are about to be discarded.
        3. ``git reset --hard FETCH_HEAD`` to match the remote tip exactly.
        4. ``git clean -fd`` to drop untracked files (ignored files are kept,
           so ``.env`` / deploy keys on the runner survive).

        Raises ``RuntimeError`` with the real git output on any failure.
        """
        if not os.path.exists(repo_path):
            raise ValueError(f"Repo path does not exist: {repo_path}")

        # Get SSH key
        if not ssh_key and workspace_name:
            key_env_name = f"{workspace_name}_SSHKEY"
            ssh_key = os.environ.get(key_env_name)
            if not ssh_key:
                raise ValueError(f"Missing env var {key_env_name} with the PRIVATE deploy key")
        elif not ssh_key:
            raise ValueError("SSH key is required. Provide it directly or set workspace_name.")

        # Normalize key
        ssh_key = self.normalize_ssh_key(ssh_key)

        # Prepare key file
        if not key_filename:
            key_filename = f"{workspace_name}_deploy" if workspace_name else "deploy_key"

        key_path = self.prepare_ssh_key(ssh_key, key_filename, ssh_dir)
        ssh_command = (
            f"ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )

        git_output = ""
        git_status = "success"
        discarded_changes: Dict[str, str] = {"working_tree_changes": "", "local_commits": ""}
        key_env_var_used = f"{workspace_name}_SSHKEY" if workspace_name else "N/A"

        original_dir = None
        try:
            original_dir = os.getcwd()
            os.chdir(repo_path)

            # 1) Fetch the target branch; FETCH_HEAD now points at the remote tip.
            fetch = self._run_git(["fetch", git_url, branch], ssh_command=ssh_command)
            if fetch.returncode != 0:
                raise RuntimeError(self._format_git_error("git fetch", fetch))

            # 2) Record what the reset is about to discard, and log it.
            discarded_changes = self._capture_local_changes()
            self._log_discarded_changes(discarded_changes)

            # 3) Force the working tree and branch pointer to match the remote.
            reset = self._run_git(["reset", "--hard", "FETCH_HEAD"])
            if reset.returncode != 0:
                raise RuntimeError(self._format_git_error("git reset --hard FETCH_HEAD", reset))

            # 4) Drop untracked files so the tree truly matches remote.
            #    -fd removes untracked files/dirs; ignored files (e.g. .env,
            #    deploy keys) are intentionally preserved (no -x).
            clean = self._run_git(["clean", "-fd"])
            if clean.returncode != 0:
                raise RuntimeError(self._format_git_error("git clean", clean))

            git_output = (reset.stdout or "") + (reset.stderr or "")
            git_status = "success"
        except Exception:
            git_status = "error"
            raise
        finally:
            if original_dir:
                try:
                    os.chdir(original_dir)
                except Exception:
                    pass
            # Clean up the key file
            try:
                os.remove(key_path)
            except Exception:
                pass

        return {
            "workspace": workspace_name or "N/A",
            "repo_path": repo_path,
            "git_pull_status": git_status,
            "git_pull_output": git_output.strip(),
            "discarded_changes": discarded_changes,
            "key_env_var_used": key_env_var_used,
        }
    
    def execute_with_alerting(
        self,
        repo_path: str,
        git_url: str,
        branch: str = "master",
        ssh_key: Optional[str] = None,
        workspace_name: Optional[str] = None,
        pipeline_uuid: str = "auto_git_pull",
        suppression_hours: int = 1,
        key_filename: Optional[str] = None,
        ssh_dir: str = "/home/src/.ssh",
        webhook_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Run :meth:`execute_git_pull` and, on failure, send a (de-duplicated)
        Slack alert before re-raising so the pipeline still fails loudly.

        ``suppression_hours`` caps how often the *same* error is alerted on,
        avoiding Slack spam when a broken state persists across many runs.
        """
        # webhook_url is a fallback only; CDM_PROD_SLACK_BOT_TOKEN still wins
        # so Mage callers that pass CDM_SLACK_WEBHOOK_URL stay on the daily thread.
        if webhook_url and not self.slack_notifier.bot_token:
            notifier = SlackNotifier(webhook_url=webhook_url)
        else:
            notifier = self.slack_notifier
        # Derive a friendly repo name (e.g. "partner-mageai") for the alert.
        repo_name = git_url.split('/')[-1].replace('.git', '')

        try:
            result = self.execute_git_pull(
                repo_path=repo_path,
                git_url=git_url,
                branch=branch,
                ssh_key=ssh_key,
                workspace_name=workspace_name,
                key_filename=key_filename,
                ssh_dir=ssh_dir
            )
            
            # Clear alert state on success
            self.alert_manager.clear_alert_state(pipeline_uuid)
            
            return result
            
        except Exception as e:
            # str(e) now carries the real git output (see _format_git_error),
            # not the opaque "returned non-zero exit status 1" message.
            git_output = str(e)
            # Truncated signature used to detect "same error as last time".
            error_signature = git_output.strip()[:500]

            # Suppress repeats of an identical error within the time window.
            should_alert, _ = self.alert_manager.should_send_alert(
                pipeline_uuid,
                error_signature,
                suppression_hours=suppression_hours
            )

            if should_alert:
                # Post to Slack (cap payload so we don't blow Slack's limits).
                notifier.send_alert(
                    repo_name,
                    git_output[:1500],
                    workspace_name=workspace_name,
                )

                # Record that we alerted so future identical errors are suppressed.
                self.alert_manager.save_alert_state(
                    pipeline_uuid,
                    error_signature,
                    datetime.now(),
                    pipeline_status="failed"
                )
            else:
                # Within suppression window — log why we stayed quiet.
                state = self.alert_manager.state_manager.load_alert_state(pipeline_uuid)
                last_alert_time = state.get("last_alert_time")
                if last_alert_time:
                    try:
                        last_alert_dt = datetime.fromisoformat(last_alert_time)
                        time_since = datetime.now() - last_alert_dt
                        print(f"🔇 Alert suppressed - same error occurred {time_since} ago (suppression window: {suppression_hours} hour(s))")
                    except Exception:
                        print(f"🔇 Alert suppressed - same error within suppression window ({suppression_hours} hour(s))")

            # Re-raise so the calling pipeline still fails.
            raise

