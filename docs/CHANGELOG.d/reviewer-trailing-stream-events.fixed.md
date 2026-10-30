- The example Claude and Codex reviewer wrappers now find the model's findings object even when
  the CLI streams further events after it. Claude Code 2.1.292 ends `stream-json` output with
  system events such as `task_summary`, which made `review run` fail with "reviewer CLI returned
  no JSON object containing findings".
