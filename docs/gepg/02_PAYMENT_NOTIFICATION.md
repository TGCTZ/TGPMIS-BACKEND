# Payment notifications

`POST /gepg/payments/notification/` stores the callback in the PostgreSQL inbox
for the periodic processor. Signature verification is
controlled by `GEPG_VERIFY_CALLBACK_SIGNATURE` and defaults to `False` to match
the legacy integration. The endpoint acknowledges only after persistence and
durable persistence succeeds.

## Parsing and settlement

`parse_payment_notification()` uses `defusedxml` and requires a `PmtHdr` and at
least one `PmtTrxDtl`. It returns the header plus transaction dictionaries,
including bill identifiers, transaction ID, amounts, currency, payment provider,
transaction timestamp, and payer fields. Exact tag mappings are in
[`gateways/gepg.py`](../../apps/billing/gateways/gepg.py).

For each transaction, `_apply_payment()`:

1. Finds the bill by `BillId`, falling back to `BillCtrNum`.
2. Creates a `Payment` keyed by `trx_id`, storing parsed fields and raw XML.
3. For a new payment, sums the bill's live payment amounts. A total covering
   the bill sets `paid`, moves its stones to `paid`, and notifies bill-paid
   subscribers. A lower cumulative total leaves the bill `pending` and its stones
   billed until later transactions cover the full amount.
4. For an existing transaction ID, updates the stored payment and returns
   without recomputing settlement or repeating the transitions.

Identical redelivery avoids a second payment row. A changed amount under an
existing transaction ID is **not** reconciled into bill status by this handler.
Settlement sums payment amounts without currency conversion or an
`is_processed` filter; this differs from the reporting balance calculation.

## Acknowledgements and transactions

The HTTP endpoint returns `pmtSpNtfReqAck` with code `7101` after it has stored
the callback. Invalid request data detected before persistence uses
`7242`; an invalid signature uses `7303` when signature verification is
enabled. Database outages return HTTP 503 so GePG can redeliver.

The PostgreSQL-backed processor applies the payment after that HTTP
acknowledgement, usually within a minute. Its business result is `7101` on
success, `7204` for an unknown bill, `7242` for invalid transaction data, or
`7321` for an internal processing failure. A
non-success business result marks the inbox event failed for staff review; it
is not a response sent back to GePG because the endpoint already acknowledged
the durable inbox record. Transient processing exceptions are retried from the
database.

Each `_apply_payment()` call is atomic and idempotent on GePG transaction ID.
Reconciliation responses use the same path, so a transaction missed by a
notification can still settle its bill without creating a duplicate payment.

Request examples are in [`api.http`](../../api.http). Use the same transaction ID
to exercise redelivery, a new ID for another payment, and a smaller amount to
settle the outstanding balance in full. Use the [simulation helper](README.md#working-offline)
when the real gateway is unavailable.

See the [production setup](04_GEPG_PRODUCTION_SETUP.md) for optional callback
signature verification, the PostgreSQL processor timer, and retry monitoring.
