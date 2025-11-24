# gitpuller

## Usage

```python
from gitpuller import GitPullExecutor

executor = GitPullExecutor(slack_webhook_url="your_webhook_url", use_mage_ai=True)
result = executor.execute_with_alerting(
    repo_path="/path/to/repo",
    git_url="git@github.com:user/repo.git",
    branch="master",
    workspace_name="workspace1"
)
```

## Build

```bash
rm -rf build dist *.egg-info
python -m build
```

Upload to PyPI and update version in `pyproject.toml`.