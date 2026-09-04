import subprocess


def deploy(branch: str) -> None:
    """Run the toy deployment program."""
    subprocess.run(f"toy-deploy --branch {branch}", check=True, shell=True)
