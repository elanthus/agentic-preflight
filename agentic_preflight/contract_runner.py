"""Bounded offline execution of untrusted probes, never in a host interpreter.

Docker is an optional, operator-managed isolation dependency. No image is pulled,
no host credentials are mounted, and no repository working-copy files are copied.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import gitx
from .config import ReviewContractsSection
from .contract_models import (
    CheckResult,
    ContractPlan,
    ContractReceipt,
    ProbeKind,
    ProbeResult,
    ReceiptStatus,
)
from .digests import json_digest
from .stages.command import resolve_on_path

MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_FILES = 4096

# This trusted root process is PID 1 inside the private container namespace. The
# probe loses root and all supplementary groups, so it cannot kill its watchdog.
# Exiting PID 1 terminates even descendants that created another process group.
WATCHDOG = r"""import hashlib,json,pathlib,signal,subprocess,sys,time
remaining = float(sys.argv[1])
deadline = time.monotonic() + remaining
signal.signal(signal.SIGALRM, lambda *_: sys.exit(124))
signal.setitimer(signal.ITIMER_REAL, max(.001,remaining))
root = pathlib.Path('/workspace')
expected = json.loads(pathlib.Path('/source-manifest.json').read_text())
actual = {}
for path in root.rglob('*'):
    if path.is_symlink() or not (path.is_file() or path.is_dir()):
        sys.exit(125)
    if path.is_file():
        actual[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
if actual != expected:
    sys.exit(125)
command = [sys.executable, "-I", "-B", "-c",
    "import runpy,sys;sys.path.insert(0,'/workspace')\n"
    "try:\n runpy.run_path('/probe.py',run_name='__main__')\n"
    "except ImportError:\n import traceback;traceback.print_exc();sys.exit(127)\n"]
child = subprocess.Popen(command, user=65534, group=65534, extra_groups=[], start_new_session=True)
try:
    result = child.wait(timeout=max(0,deadline-time.monotonic()))
except subprocess.TimeoutExpired:
    result = 124
sys.exit(result if result >= 0 else 128 - result)
"""


@dataclass
class Capture:
    exit_code: int
    stdout: bytes
    stderr: bytes
    reason: str = ""


def _capture(argv: list[str], timeout: float, limit: int, *, cwd: Path) -> Capture:
    """Drain both pipes concurrently, retaining at most limit bytes in total."""
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()
    lock = threading.Lock()
    with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:

        def drain(index: int) -> None:
            stream = process.stdout if index == 0 else process.stderr
            if stream is None:
                return
            while data := stream.read(4096):
                with lock:
                    room = max(0, limit - sum(map(len, buffers)))
                    buffers[index].extend(data[:room])
                    if len(data) > room:
                        exceeded.set()

        threads = [threading.Thread(target=drain, args=(i,), daemon=True) for i in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + max(0, timeout)
        reason = ""
        while process.poll() is None:
            if exceeded.is_set() or time.monotonic() >= deadline:
                reason = "output limit exceeded" if exceeded.is_set() else "execution timed out"
                process.kill()
                break
            time.sleep(0.01)
        process.wait()
        for thread in threads:
            thread.join(timeout=1)
        if exceeded.is_set():
            reason = "output limit exceeded"
        return Capture(process.returncode, bytes(buffers[0]), bytes(buffers[1]), reason)


def _git_blob(repo: Path, args: list[str], timeout: float) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(
        [gitx._git_executable(), "--no-replace-objects", "-C", str(repo), *args],
        capture_output=True,
        env=environment,
        timeout=max(0.01, timeout),
        check=True,
    )
    return result.stdout


def _portable_name(name: str) -> bool:
    devices = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    return all(
        part not in {"", ".", ".."}
        and part.casefold() != ".git"
        and not part.endswith((".", " "))
        and not any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        and part.split(".", 1)[0].upper() not in devices
        for part in name.split("/")
    )


def export_snapshot(repo: Path, head: str, destination: Path, deadline: float) -> str:
    """Export regular committed blobs; reject unsupported trees before running code."""
    listing = _git_blob(repo, ["ls-tree", "-rlz", head], deadline - time.monotonic())
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(entries) > MAX_SNAPSHOT_FILES:
        raise ValueError("snapshot file budget exceeded")
    files = []
    total = 0
    seen: dict[str, str] = {}
    for entry in entries:
        metadata, raw_path = entry.split(b"\t", 1)
        mode, kind, oid, size = metadata.split()
        name = raw_path.decode("utf-8")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or PureWindowsPath(name).drive
            or "\\" in name
            or not _portable_name(name)
        ):
            raise ValueError("snapshot has an unsafe or unsupported path")
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise ValueError("contract snapshots do not support symlinks or submodules")
        for parent in (*path.parents, path):
            text = parent.as_posix()
            previous = seen.setdefault(text.casefold(), text)
            if previous != text:
                raise ValueError("snapshot has case-colliding paths")
        total += int(size)
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot byte budget exceeded")
        files.append((path, oid.decode("ascii"), int(size), mode))
    destination.mkdir()
    for path, oid_text, size_value, mode in files:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("total execution budget exhausted during snapshot export")
        data = _git_blob(repo, ["cat-file", "blob", oid_text], remaining)
        if len(data) != size_value:
            raise ValueError("snapshot blob size changed")
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o555 if mode == b"100755" else 0o444)
    destination.chmod(0o755)
    for child in destination.rglob("*"):
        if child.is_dir():
            child.chmod(0o755)
    return hashlib.sha256(listing).hexdigest()


def container_argv(
    docker: str,
    image: str,
    name: str,
    snapshot: Path,
    probe: Path,
    watchdog: Path,
    source_manifest: Path,
    timeout: float,
) -> list[str]:
    """All execution restrictions are code-owned, never supplied by the plan."""
    if any(
        "," in str(path) or "\n" in str(path)
        for path in (snapshot, probe, watchdog, source_manifest)
    ):
        raise ValueError("container bind paths cannot contain mount-option separators")
    return [
        docker,
        "run",
        "--name",
        name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--cap-add=SETUID",
        "--cap-add=SETGID",
        "--cap-add=KILL",
        "--security-opt=no-new-privileges",
        "--user=0:0",
        "--cpus=1",
        "--memory=256m",
        "--memory-swap=256m",
        "--pids-limit=64",
        "--ulimit=nofile=256:256",
        "--log-driver=none",
        "--env=HOME=/tmp",
        "--env=TMPDIR=/tmp",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=64m,mode=1777",
        "--mount",
        f"type=bind,src={snapshot},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={probe},dst=/probe.py,readonly",
        "--mount",
        f"type=bind,src={watchdog},dst=/watchdog.py,readonly",
        "--mount",
        f"type=bind,src={source_manifest},dst=/source-manifest.json,readonly",
        "--workdir=/workspace",
        "--entrypoint=python",
        image,
        "-I",
        "-B",
        "/watchdog.py",
        str(timeout),
    ]


def _probe(
    docker: str,
    config: ReviewContractsSection,
    snapshot: Path,
    code: str,
    kind: ProbeKind,
    deadline: float,
    temporary: Path,
) -> ProbeResult:
    started = time.monotonic()
    remaining = min(config.timeout_seconds, deadline - started)
    if remaining <= 0:
        return ProbeResult(
            kind=kind, status="unverified", reason="total execution budget exhausted"
        )
    name = "ap-contract-" + uuid.uuid4().hex
    probe = temporary / f"{name}.py"
    probe.write_text(code, encoding="utf-8")
    probe.chmod(0o444)
    watchdog = temporary / f"{name}-watchdog.py"
    watchdog.write_text(WATCHDOG, encoding="utf-8")
    watchdog.chmod(0o444)
    result = ProbeResult(kind=kind, status="unverified", container_name=name)
    try:
        capture = _capture(
            container_argv(
                docker,
                config.image or "",
                name,
                snapshot,
                probe,
                watchdog,
                temporary / "source-manifest.json",
                remaining,
            ),
            remaining,
            config.max_output_bytes,
            cwd=temporary,
        )
        unverified = bool(capture.reason) or capture.exit_code in {124, 125, 126, 127, 137, 143}
        result = ProbeResult(
            kind=kind,
            status="unverified" if unverified else "passed" if capture.exit_code == 0 else "failed",
            exit_code=capture.exit_code,
            reason=capture.reason or ("runtime or resource failure" if unverified else ""),
            stdout=capture.stdout.decode("utf-8", errors="replace"),
            stderr=capture.stderr.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - started,
            container_name=name,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result.reason = str(exc)[:2000]
    finally:
        # Killing the attached client alone leaves the container running.
        try:
            cleanup = _capture([docker, "rm", "--force", name], 2, 4096, cwd=temporary)
            result.cleanup_succeeded = cleanup.exit_code == 0 and not cleanup.reason
        except (OSError, subprocess.SubprocessError):
            result.cleanup_succeeded = False
        # The in-container watchdog independently terminates the PID namespace.
    return result


def execute_plan(
    repo: Path,
    head: str,
    plan: ContractPlan,
    config: ReviewContractsSection,
) -> ContractReceipt:
    """Run a validated plan; unavailable execution stays explicitly unverified."""
    deadline = time.monotonic() + config.total_timeout_seconds
    docker = resolve_on_path("docker")
    reason = ""
    snapshot_digest = None
    results: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="ap-contract-") as folder:
        temporary = Path(folder).resolve()
        snapshot = temporary / "snapshot"
        if plan.checks:
            try:
                if docker is None or config.image is None:
                    raise ValueError("Docker and a locally available pinned image are required")
                snapshot_digest = export_snapshot(repo, head, snapshot, deadline)
                expected = {
                    path.relative_to(snapshot).as_posix(): hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                    for path in snapshot.rglob("*")
                    if path.is_file()
                }
                source_manifest = temporary / "source-manifest.json"
                source_manifest.write_text(json.dumps(expected), encoding="utf-8")
                source_manifest.chmod(0o444)
            except (ValueError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
                reason = str(exc)[:2000]
        for check in plan.checks:
            probes = [
                ProbeResult(kind=probe.kind, status="unverified", reason=reason)
                if reason
                else _probe(
                    docker or "", config, snapshot, probe.code, probe.kind, deadline, temporary
                )
                for probe in check.probes
            ]
            results.append(CheckResult(id=check.id, probes=probes))
    statuses = {probe.status for check in results for probe in check.probes}
    if any(unit.disposition == "unverified" for unit in plan.units) or any(
        item.disposition == "unverified"
        for unit in plan.units
        for behavior in unit.inventory
        for item in behavior.input_classes
    ):
        statuses.add("unverified")
    status: ReceiptStatus = (
        "unverified"
        if "unverified" in statuses
        else "failed"
        if "failed" in statuses
        else "passed"
        if results
        else "not_applicable"
    )
    return ContractReceipt(
        manifest=plan.manifest,
        head_sha=head,
        config_sha256=json_digest(config.model_dump(mode="json")),
        plan_sha256=json_digest(plan.model_dump(mode="json")),
        image=config.image,
        snapshot_sha256=snapshot_digest,
        status=status,
        plan=plan,
        checks=results,
    )
