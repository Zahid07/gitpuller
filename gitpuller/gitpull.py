import os
import subprocess
from typing import Any, Dict


def git_pull(repo_path: str, workspace_name: str, git_url: str, key_material: str) -> Dict[str, Any]:
    """
    Executes 'git pull' in the specified repository path using a deploy key
    provided via the env var WP_AUTOPULL_SSHKEY.

    Args:
        repo_path: Path to the local git repository
        workspace_name: Logical workspace identifier
        git_url: Git remote URL (SSH-based)
    Returns:
        Dict with status and command output.
    """
    print(f"Running git pull for workspace: {workspace_name}")
    print(f"Repo path: {repo_path}")

    if not os.path.exists(repo_path):
        raise FileNotFoundError(f"Repo path does not exist: {repo_path}")


    if not key_material:
        raise EnvironmentError(f"Missing SSH key material for workspace: {workspace_name}")

    if "\\n" in key_material and "\n" not in key_material:
        key_material = key_material.replace("\\n", "\n")

    ssh_dir = os.path.expanduser("~/.ssh")
    key_path = os.path.join(ssh_dir, "gitpuller_deploy_key")
    os.makedirs(ssh_dir, exist_ok=True)
    os.chmod(ssh_dir, 0o700)

    with open(key_path, "w") as f:
        f.write(key_material)
    os.chmod(key_path, 0o600)

    cmd = [
        "git",
        "-c",
        f"core.sshCommand=ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
        "pull",
        git_url,
        "master",
    ]

    try:
        os.chdir(repo_path)
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        git_output = result.stdout
        git_status = "success"
    except subprocess.CalledProcessError as e:
        git_output = (e.stdout or "") + (e.stderr or "")
        git_status = "error"
    finally:
        try:
            os.remove(key_path)
        except Exception:
            pass

    return {
        "workspace": workspace_name,
        "repo_path": repo_path,
        "git_pull_status": git_status,
        "git_pull_output": git_output.strip(),
    }
