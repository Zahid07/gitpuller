"""gitpuller — auto-pull a git repo over SSH inside Mage pipelines.

Public API is re-exported here so callers can ``from gitpuller import ...``.
"""

from .gitpull import GitPullExecutor
from .alert_manager import AlertManager
from .state_manager import StateManager, InMemoryStateManager, MageAIStateManager
from .slack_notifier import SlackNotifier
from .utils import transform_custom, get_repo_path, get_env_base_path


__version__ = "1.1.0"
__all__ = [
    "GitPullExecutor",
    "AlertManager",
    "StateManager",
    "InMemoryStateManager",
    "MageAIStateManager",
    "SlackNotifier",
    "transform_custom", 
    "get_repo_path", 
    "get_env_base_path"
]