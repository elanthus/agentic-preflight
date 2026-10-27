- Added a JSON Schema for the stdout envelope and for each command's `data` object under
  `skill/reference/schema/`, generated from typed models by `tools/generate_schema.py`.
  Payloads are unchanged; tests keep the schemas and the command reference in step.
