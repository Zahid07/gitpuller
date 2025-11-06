import os


def get_repo_path() -> str:
    """
    Returns the current working repository path (directory of this script).
    """
    return os.getcwd()


def get_env_base_path() -> str:
    """
    Returns the value of the ENV environment variable.
    """
    return os.environ.get("ENV", "")


def transform_custom() -> tuple[str, str]:
    """
    Returns a tuple of (repo_path, base_path)
    """
    repo_path = get_repo_path()
    base_path = get_env_base_path()

    if not repo_path:
        raise Exception("Unable to determine repository path.")

    return repo_path, base_path


def test_output(output) -> None:
    """
    Simple test function to validate non-empty output.
    """
    assert output is not None, "Output is undefined"
