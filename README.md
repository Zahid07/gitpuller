# gitpuller

This is how to use this

from gitpuller import git_pull, transform_custom

repo_path, base_path = transform_custom()
result = git_pull(repo_path, "workspace1", "git@github.com:youruser/yourrepo.git", "1234")
print(result)


## New Build

Clean up the orignal build

rm -rf build dist *.egg-info

Make a new build

python -m build

Then Upload to PyPi and dont forget to change the version in pyproject.toml