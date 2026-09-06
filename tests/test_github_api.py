"""The live transport consumes bounded API JSON, never executable artifacts."""

import base64
import json
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agentic_preflight import github_api
from agentic_preflight.github_api import APIUnavailable, GitHub


def test_rest_transport_uses_structured_stdin_and_never_prints_credentials(monkeypatch):
    calls = []

    def invoke(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"id":1}', stderr="")

    monkeypatch.setattr(subprocess, "run", invoke)
    api = GitHub("owner/repo")
    payload = {"inputs": {"candidate": '$(unsafe) `unsafe` "quoted"\nline'}}
    assert api.request("actions/workflows/1/dispatches", method="POST", body=payload) == {"id": 1}
    args, kwargs = calls[0]
    assert args == [
        "gh",
        "api",
        "--method",
        "POST",
        "repos/owner/repo/actions/workflows/1/dispatches",
        "--input",
        "-",
    ]
    assert json.loads(kwargs["input"]) == payload
    assert not kwargs.get("shell")
    assert kwargs["timeout"] == 60
    assert api.scoped("contributor/fork").repository == "contributor/fork"


@pytest.mark.parametrize("failure", ["exit", "json", "timeout", "missing"])
def test_transport_errors_remain_unavailable_without_stderr_secrets(monkeypatch, failure):
    def invoke(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("gh", 60)
        if failure == "missing":
            raise FileNotFoundError("gh")
        return SimpleNamespace(
            returncode=1 if failure == "exit" else 0,
            stdout="malformed",
            stderr="sensitive diagnostic",
        )

    monkeypatch.setattr(subprocess, "run", invoke)
    with pytest.raises(APIUnavailable) as caught:
        GitHub("owner/repo").request("actions/runs/1")
    assert "sensitive diagnostic" not in str(caught.value)


def test_paginated_response_is_complete_and_bounded(monkeypatch):
    api = GitHub("owner/repo")
    pages = [
        {"total_count": 101, "jobs": [{"id": n} for n in range(100)]},
        {"total_count": 101, "jobs": [{"id": 100}]},
    ]
    monkeypatch.setattr(api, "request", lambda path: pages.pop(0))
    assert len(api.pages("actions/runs/1/attempts/2/jobs", "jobs")) == 101
    monkeypatch.setattr(
        api, "request", lambda path: {"total_count": 2000, "jobs": [{"id": n} for n in range(100)]}
    )
    with pytest.raises(APIUnavailable, match="bounded"):
        api.pages("actions/runs/1/attempts/2/jobs", "jobs")


@pytest.mark.parametrize(
    "response", [{"total_count": 3, "jobs": [{"id": 1}]}, {"jobs": "bad"}, {"jobs": ["bad"]}]
)
def test_partial_or_malformed_pages_fail_closed(monkeypatch, response):
    api = GitHub("owner/repo")
    monkeypatch.setattr(api, "request", lambda path: response)
    with pytest.raises(APIUnavailable):
        api.pages("jobs", "jobs")


def test_note_retrieval_uses_git_objects_and_handles_fanout(monkeypatch):
    api = GitHub("owner/repo")
    head = "a" * 40
    answers = {
        "git/ref/notes/agentic-preflight": {"object": {"sha": "note-commit"}},
        "git/commits/note-commit": {"tree": {"sha": "note-tree"}},
        "git/trees/note-tree?recursive=1": {
            "tree": [{"type": "blob", "path": "aa/" + "a" * 38, "sha": "blob"}]
        },
        "git/blobs/blob": {
            "encoding": "base64",
            "content": base64.b64encode(b'{"note":"inert"}').decode(),
        },
    }
    monkeypatch.setattr(api, "request", lambda path: answers[path])
    assert api.note(head) == '{"note":"inert"}'
    answers["git/trees/note-tree?recursive=1"]["truncated"] = True
    with pytest.raises(APIUnavailable, match="truncated"):
        api.note(head)
    answers["git/trees/note-tree?recursive=1"]["truncated"] = False
    answers["git/trees/note-tree?recursive=1"]["tree"] = []
    with pytest.raises(APIUnavailable, match="no unique"):
        api.note(head)


def test_protected_contents_are_decoded_as_data(monkeypatch):
    api = GitHub("owner/repo")
    seen = []

    def request(path):
        seen.append(path)
        return {"encoding": "base64", "content": base64.b64encode(b"policy").decode()}

    monkeypatch.setattr(api, "request", request)
    assert api.contents(".agentic-preflight.toml", "a" * 40) == "policy"
    assert seen == ["contents/.agentic-preflight.toml?ref=" + "a" * 40]
    monkeypatch.setattr(api, "request", lambda path: {"encoding": "none"})
    with pytest.raises(APIUnavailable, match="protected file"):
        api.contents(".agentic-preflight.toml", "a" * 40)


@pytest.mark.parametrize(
    "repository", ["../repo", "owner/repo/extra", "https://evil/repo", "owner/$(run)"]
)
def test_repository_is_not_an_arbitrary_url_or_command(repository):
    with pytest.raises(ValueError, match="OWNER/REPO"):
        GitHub(repository)


def test_public_fork_fallback_sends_no_base_installation_credentials(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="forbidden"),
    )
    seen = []

    @contextmanager
    def opened(request, **kwargs):
        seen.append(request)
        yield SimpleNamespace(read=lambda limit: b'{"public":true}')

    monkeypatch.setattr(github_api, "urlopen", opened)
    api = GitHub("contributor/fork", public=True)
    assert api.request("git/ref/notes/agentic-preflight") == {"public": True}
    assert not seen[0].has_header("Authorization")
    assert seen[0].full_url.startswith("https://api.github.com/repos/contributor/fork/")
    with pytest.raises(ValueError, match="read-only"):
        api.request("check-runs", method="POST", body={})

    @contextmanager
    def bad(request, **kwargs):
        yield SimpleNamespace(read=lambda limit: b"x" * limit)

    monkeypatch.setattr(github_api, "urlopen", bad)
    with pytest.raises(APIUnavailable, match="public fork API unavailable"):
        api.request("git/ref/notes/agentic-preflight")
