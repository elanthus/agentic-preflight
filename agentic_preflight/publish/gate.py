"""The confirmation gate.

Be honest about what this is: **the token is not a security boundary.** The
agent can read it straight out of `status`. It is deliberate ceremony that makes
an *accidental* push impossible and makes an unconfirmed push a visible protocol
violation rather than an invisible one.

``gate.mode = "manual"`` is the honest answer for anyone who needs a real
boundary: it refuses to proceed at all, so a person must type the push command
themselves.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field


@dataclass
class GateSummary:
    remote: str
    refspec: str
    branch: str
    base_ref: str
    commits: list[dict] = field(default_factory=list)
    token: str = ""

    def as_dict(self) -> dict:
        return {
            "remote": self.remote,
            "refspec": self.refspec,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "commits": self.commits,
            "token": self.token,
        }


def mint_token() -> str:
    return secrets.token_hex(8)


def token_matches(expected: str | None, supplied: str | None) -> bool:
    if not expected or not supplied:
        return False
    return secrets.compare_digest(expected, supplied)
