import subprocess


def deploy(branch: str) -> None:
    """Run the toy deployment program without a shell."""
    subprocess.run(["toy-deploy", "--branch", branch], check=True)
