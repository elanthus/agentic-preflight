- The command reviewer (the packaged `reviewer run` and the example wrappers) sends the
  prompt to the provider CLI as UTF-8 and decodes the CLI's output as UTF-8, replacing
  undecodable bytes, instead of using the locale encoding. A non-ASCII diff under an
  ASCII or cp1252 locale no longer crashes the reviewer with a traceback.
- An `AP_REVIEWER_EVENTS_FILE` that cannot be written, such as a path in a missing or
  read-only directory, now fails the review with one `independent reviewer failed: ...`
  line instead of a traceback or a misleading "reviewer CLI not found" message.
