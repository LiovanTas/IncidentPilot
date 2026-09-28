# Slow queries and missing indexes

## Symptoms

- p99 latency rises sharply and stays high; p50 rises modestly. Throughput falls.
- Database CPU and disk read IOPS climb together.
- The slow-query log fills with one statement shape repeated thousands of times.
- Downstream services start timing out against the affected service before it reports errors itself.

## Likely causes

1. A migration dropped or renamed an index that a hot query depended on.
2. A new query shape was introduced that has no supporting index (a new filter column, a new sort).
3. An ORM change turned one query into many (see `orm-n-plus-one`).
4. Data growth crossed the point where a sequential scan stopped being cheap.

## Diagnosis

- `EXPLAIN ANALYZE` the top statement from the slow-query log. `Seq Scan` on a large table with a
  selective filter is the signature.
- Diff any migration that landed in the blast window. Look for `DROP INDEX`, `ALTER TABLE`,
  a renamed column, or a new `CREATE TABLE` without indexes.
- A migration whose *up* step creates an index `CONCURRENTLY` may have failed silently and left the
  index invalid. Check `pg_index.indisvalid`.

## Mitigation

1. Recreate the index concurrently so the rebuild does not lock writes:
   `CREATE INDEX CONCURRENTLY idx_name ON table (column);`
2. If the query is new and unnecessary, feature-flag it off rather than waiting for the index build.
3. Shed load on the affected endpoint if the database is saturated; an index build competes for the
   same IO.

## Rollback

Reverting application code does not undo a migration. Revert the code that issues the query, then
restore the index. Never roll a destructive migration backwards under load.

## Verification

- Slow-query log quiet for 5 minutes.
- p99 within 20% of the pre-incident value.
- `EXPLAIN` shows an index scan for the offending statement.
