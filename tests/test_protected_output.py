"""Shared output protection keeps raw protocol streams separate from safe reports."""

import pytest

from agentic_preflight.stages import shellstage
from agentic_preflight.stages.protected_output import OutputProtection


def test_capture_rejects_unreadable_secret_text(tmp_path):
    copied = tmp_path / ".env"
    copied.write_bytes(b"TOKEN=\xff\n")
    with pytest.raises(shellstage.SecretRedactionError) as raised:
        OutputProtection.capture(tmp_path, [".env"])
    assert raised.value.path == copied


def test_redaction_combines_snapshots_and_preserves_protocol_streams(tmp_path):
    copied = tmp_path / ".env"
    copied.write_text("TOKEN=before-secret\n", encoding="utf-8")
    protection = OutputProtection.capture(tmp_path, [".env"])
    copied.write_text("TOKEN=after-secret\n", encoding="utf-8")
    result = shellstage.StageResult(
        command="review",
        exit_code=0,
        output="before-secret\nafter-secret\n",
        stdout="before-secret\n",
        stderr="after-secret\n",
    )
    protected = protection.finish(result, tmp_path / "logs" / "review.txt")
    assert protected.result is result
    assert protected.result.stdout == "before-secret\n"
    assert protected.result.stderr == "after-secret\n"
    assert protected.clean_output == "[redacted]\n[redacted]\n"
    assert protected.log_path.read_bytes() == protected.clean_output.encode("utf-8")
    assert protected.failure_reason is None


@pytest.mark.parametrize("unreadable", [False, True])
@pytest.mark.parametrize("exit_code", [0, 7, 124])
def test_unsafe_output_discards_all_streams_and_preserves_failure_metadata(
    tmp_path, unreadable, exit_code
):
    copied = tmp_path / ".env"
    copied.write_text("TOKEN=original-secret\n", encoding="utf-8")
    protection = OutputProtection.capture(tmp_path, [".env"])
    if unreadable:
        copied.write_bytes(b"\xff")
    result = shellstage.StageResult(
        command="review",
        exit_code=exit_code,
        output="transient-secret",
        stdout="transient-secret",
        stderr="transient-secret",
        timed_out=exit_code == 124,
        copied_files_changed=True,
    )
    protected = protection.finish(result, tmp_path / "log.txt")
    assert not protected.result.passed
    assert protected.result.exit_code == (exit_code or 1)
    assert protected.result.timed_out == result.timed_out
    assert protected.result.copied_files_changed
    assert protected.result.stdout is None
    assert protected.result.stderr is None
    assert protected.result.output == shellstage.REDACTION_FAILURE_OUTPUT
    assert protected.log_path.read_bytes() == shellstage.REDACTION_FAILURE_OUTPUT.encode("utf-8")
    assert protected.failure_reason == (
        "copied-file redaction became unavailable"
        if unreadable
        else "copied file changed during command execution"
    )
    assert (protected.redaction_error is not None) is unreadable
