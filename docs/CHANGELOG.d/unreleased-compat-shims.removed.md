- Remove three compatibility paths that guarded data no release wrote: the schema-1
  contract plan and receipt readers, and the serializers that kept `producer` and a
  stage's `started_at` out of rewritten run records. A schema-1 receipt is now reported
  as unreadable and left in place, and a rewritten record stores `producer` and
  `started_at` as `null` when they are unknown.
