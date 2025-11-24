import os
import subprocess
from typing import Dict, Any, Optional
from datetime import datetime
from .alert_manager import AlertManager
from .slack_notifier import SlackNotifier

class GitPullExecutor:    
    def __init__(
        self,
        slack_webhook_url: Optional[str] = None,
        use_mage_ai: bool = False,
        state_manager: Optional[Any] = None
    ):
        self.slack_notifier = SlackNotifier(webhook_url=slack_webhook_url)
        self.alert_manager = AlertManager(state_manager=state_manager, use_mage_ai=use_mage_ai)
    
    def normalize_ssh_key(self, key_material: str) -> str:
        if not key_material:
            return key_material
        
        # Remove any quotes that might be wrapping the key
        key_material = key_material.strip().strip('"').strip("'")
        
        # Handle literal \n sequences
        if "\\n" in key_material:
            key_material = key_material.replace("\\n", "\n")
        
        # Ensure proper line endings
        if not key_material.endswith("\n"):
            key_material += "\n"
        
        return key_material
    
    def prepare_ssh_key(
        self, 
        key_material: str, 
        key_filename: str = "deploy_key",
        ssh_dir: str = "/home/src/.ssh"
    ) -> str:
        os.makedirs(ssh_dir, exist_ok=True)
        os.chmod(ssh_dir, 0o700)
        
        key_path = os.path.join(ssh_dir, key_filename)
        
        # Write key with restricted permissions
        with open(key_path, "w") as f:
            f.write(key_material)
        os.chmod(key_path, 0o600)
        
        return key_path
    
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
        
        # Execute git pull
        cmd = [
            "git",
            "-c",
            f"core.sshCommand=ssh -i {key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
            "pull",
            git_url,
            branch,
        ]
        
        git_output = ""
        git_status = "success"
        key_env_var_used = f"{workspace_name}_SSHKEY" if workspace_name else "N/A"
        
        original_dir = None
        try:
            original_dir = os.getcwd()
            os.chdir(repo_path)
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            git_output = result.stdout
            git_status = "success"
        except subprocess.CalledProcessError as e:
            git_output = (e.stdout or "") + (e.stderr or "")
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
        ssh_dir: str = "/home/src/.ssh"
    ) -> Dict[str, Any]:
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
            git_output = str(e)
            error_signature = git_output.strip()[:500]
            
            # Check if we should send alert
            should_alert, _ = self.alert_manager.should_send_alert(
                pipeline_uuid,
                error_signature,
                suppression_hours=suppression_hours
            )
            
            if should_alert:
                # Send alert
                self.slack_notifier.send_alert(repo_name, git_output[:1500])
                
                # Save alert state
                self.alert_manager.save_alert_state(
                    pipeline_uuid,
                    error_signature,
                    datetime.now(),
                    pipeline_status="failed"
                )
            else:
                # Alert suppressed
                state = self.alert_manager.state_manager.load_alert_state(pipeline_uuid)
                last_alert_time = state.get("last_alert_time")
                if last_alert_time:
                    try:
                        from datetime import datetime, timedelta
                        last_alert_dt = datetime.fromisoformat(last_alert_time)
                        time_since = datetime.now() - last_alert_dt
                        print(f"🔇 Alert suppressed - same error occurred {time_since} ago (suppression window: {suppression_hours} hour(s))")
                    except:
                        print(f"🔇 Alert suppressed - same error within suppression window ({suppression_hours} hour(s))")
            
            # Re-raise the exception
            raise

