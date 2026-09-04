import subprocess


def deploy(branch: str) -> None:
    """Run one toy deployment."""
    subprocess.run(["toy-deploy", "--branch", branch], check=True)
