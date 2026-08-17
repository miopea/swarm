# Swarm Next migration finalization

Task import and Legacy cleanup are deliberately separate operations.

1. Swarm Next exports a read-only Legacy database snapshot, previews the package,
   and imports an explicit selection as Draft work.
2. The operator verifies those tasks in Next and downloads its content-bound receipt.
3. With Swarm Legacy briefly stopped, `swarm migration preview BUNDLE RECEIPT`
   proves the receipt, Hive identity, bundle digest, and every selected source
   record still match.
4. `swarm migration finish BUNDLE RECEIPT` creates a SQLite safety backup and
   marks only the proven source tasks `Moved to Swarm Next`. They remain visible
   but read-only and are **not** changed to Done. Their former worker assignment
   is released. An append-only `MIGRATED` audit event records the Next batch.
5. `swarm migration reverse BATCH_ID` restores the source tasks only when none
   has changed since finalization. The reversal is itself audited.

The daemon-offline requirement is intentional. A running Legacy daemon owns an
in-memory task board and could otherwise persist stale state over a correct
finalization. This brief stop does not affect Swarm Next workers or its separate
worker engine.

There is no dual write. After finalization, Next owns the imported work. The
Legacy receipt tables exist only to prove and, while untouched, reverse the
handoff.
