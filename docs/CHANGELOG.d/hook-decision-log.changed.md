- The installed pre-push hook passes the remote name to `hook-check` in the
  `AGENTIC_PREFLIGHT_PUSH_REMOTE` environment variable, which older versions ignore.
  `init` now replaces a pre-push hook that is exactly one an earlier version installed;
  any other hook is still left unchanged.
