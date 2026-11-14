#!/usr/bin/env python3
"""Deprecated pointer to the packaged codex reviewer.

The reviewer moved into the package. Configure this instead:

    command = "agentic-preflight reviewer run --provider codex"

This file stays for one release so that existing commands keep working. It needs an
interpreter that can import agentic_preflight. See docs/independent-review.md.
"""

from __future__ import annotations

import sys

try:
    from agentic_preflight.reviewers.codex import main
except ImportError:
    print(
        "independent reviewer failed: this example now delegates to the agentic_preflight "
        "package, which this interpreter cannot import; configure "
        '"agentic-preflight reviewer run --provider codex" instead',
        file=sys.stderr,
    )
    raise SystemExit(1) from None

if __name__ == "__main__":
    raise SystemExit(main())
