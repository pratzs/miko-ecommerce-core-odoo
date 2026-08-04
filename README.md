# E-Commerce Connector Engine (Miko)

The shared engine every Miko store connector runs on: identity mapping so a re-run
never duplicates an order, a job queue that keeps failures instead of losing them
and can actually re-run them, and the store record itself.

Free, LGPL-3, Odoo 16.0 to 19.0. On its own it does nothing visible, which is
deliberate: it is the engine, not the car. Install a platform connector alongside
it, such as **Shopify Odoo Connector (Miko)**.

| | |
|---|---|
| Module | `miko_ecommerce_core` |
| Series | 16.0, 17.0, 18.0, 19.0 |
| Licence | LGPL-3 |
| Tests | 21, all four series |

## What it provides

- **`miko.ecommerce.mapping`** — the table that makes an import an upsert. Unique
  index created in `init()` so it exists on every series, including Odoo 19 where
  `_sql_constraints` is no longer honoured.
- **`miko.ecommerce.job`** — a queue that is actually drained. Jobs dispatch back to
  `_job_<operation>` on the channel, so Retry re-runs the code that failed against
  the payload that failed. Failures split into transient (retried with backoff) and
  blocked (needs a person, never retried on a timer, carries the fix).
- **`miko.ecommerce.channel`** — the connected store, with safe defaults: nothing is
  confirmed, invoiced or pushed until somebody switches it on.

Support: support@tripsterdevelopers.com
