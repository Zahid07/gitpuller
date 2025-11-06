# gitpuller

This is how to use this

from gitpuller import git_pull, transform_custom

repo_path, base_path = transform_custom()
result = git_pull(repo_path, "workspace1", "git@github.com:youruser/yourrepo.git", "1234")
print(result)