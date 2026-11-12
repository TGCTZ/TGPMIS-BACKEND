# GePG integration

The gateway submits bills, returns control numbers, and exchanges payment,
cancellation, and reconciliation messages. These pages describe this adapter,
not the full GePG protocol.

| Guide | Purpose |
| --- | --- |
| [Overview](00_GEPG_INTEGRATION_OVERVIEW.md) | Modules, configuration, and current limitations |
| [Historical flow diagrams](03_GEPG_FLOW_DIAGRAMS.md) | October 8 implementation snapshot; use the current guides below for present behavior |
| [Bill submission](01_BILL_SUBMISSION.md) | Local billing, submission, retries, and control-number callbacks |
| [Payment notifications](02_PAYMENT_NOTIFICATION.md) | Parsing, settlement, acknowledgements, and duplicate handling |
| [Production setup](04_GEPG_PRODUCTION_SETUP.md) | VPS secrets, PFX provisioning, callback routing, and pre-cutover checks |

## Working offline

`GEPG_SIMULATE=True` makes outbound submission return a generated control number
without calling GePG. This gateway switch itself does not check `DEBUG`.
The API's `POST /api/v1/bills/{id}/simulate-payment/` action requires **both**
`DEBUG` and `GEPG_SIMULATE`, plus `billing.generate_bill`.

Simulation feeds generated XML through the payment handler and pays the
outstanding balance in full.
See [`apps/billing/dev.py`](../../apps/billing/dev.py).

## Current limitations

- Outbound signing is required in production. Inbound callback verification is
  controlled by `GEPG_VERIFY_CALLBACK_SIGNATURE` and defaults to `False` to
  match the legacy integration; a public certificate is required only when
  verification is enabled. `GEPG_COM` is required and `GEPG_ALG` is optional.
- Callback processing and outbound retries run from PostgreSQL inbox/outbox
  records through a once-per-minute systemd timer. Each pass is capped at 45
  seconds; remaining records wait in PostgreSQL for the next pass. Application
  rejections are not automatically retried.
- Accepted asynchronous submissions without a control number wait for the
  callback and are not outbound retry candidates.
- A non-simulated control-number request received a GePG reply on 2026-10-14.
  A bill is issued a number only after a successful `billSubRes` with
  `BillStsCode=7101` and a nonzero `BillCntrNum`; an accepted `billSubReqAck`
  (`7101`) remains pending until its callback arrives.
- Cancellation and reconciliation API actions are implemented, but their live
  GePG flows have not yet been confirmed.
- Beem SMS is sent when a control number is available; delivery depends on
  provider availability and configured credentials.

These limitations describe running code; they are not guarantees of gateway
compatibility or authentication.
