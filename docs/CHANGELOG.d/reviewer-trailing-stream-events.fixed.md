- The example Claude reviewer wrapper now reads findings from the last `result` envelope even
  when Claude Code streams further events after it. Claude Code 2.1.292 ends `stream-json`
  output with system events such as `task_summary`, which made `review run` fail with
  "reviewer CLI returned no JSON object containing findings". A result envelope marked
  `is_error` now fails the review with its subtype instead of falling back to another object.
