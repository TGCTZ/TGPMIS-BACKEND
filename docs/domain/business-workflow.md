# Business workflow

This describes the implemented workflow. API paths below are relative to
`/api/v1/`. See [business rules](decisions.md) for the model constraints and
[permissions](../engineering/permissions.md) for access control.

## Intake, billing, findings, and collection

1. **Reception creates an order** with a customer, received date, and submitted
   stone count. A customer can be selected, created with the order, or managed
   separately through the Customers API. Order creation does not create stones.
2. **Preliminary identification registers each stone's pricing category** through
   `POST /orders/{id}/stones/`. An optional exact type must belong to that
   category; older callers can supply a type alone to derive the category.
   The endpoint requires `orders.add_stone`; the receptionist role does not have it.
3. **Billing prices the registered stones** at the flat `StoneCategory.price`.
   There is one bill per order and one line per stone. Missing prices prevent
   bill creation. Line amounts are copied at creation, so later catalogue edits
   do not reprice an existing bill. Weight and exact type are not needed to price it.
4. **GePG confirms payment.** Underpaid transactions are recorded, but the bill
   stays `pending` until recorded payments cover the full total. Then the bill
   becomes `paid` and its stones move to `paid`. Findings services require a
   fully paid bill.
5. **The bench records findings** against each stone: exact type, weight, lookup
   selections, measurements, comments, and instruments. Drafts may be incomplete.
   Finalization requires species, variety, colour, shape/cut, nonzero weight,
   transparency, optic character, nature, and a photograph. Comments remain
   optional. A distinct second signatory from the active gemmologist list is
   also required.
6. **Finalizing through the API also issues the certificate**, in one transaction.
   If issuance fails, finalization rolls back. The standalone certificate API
   remains available for a finalized, paid stone without a certificate.
7. **Certificates can be viewed, verified publicly, and downloaded as PDFs.**
   When the issuance service finds no stones outside `certified` or `cancelled`,
   it notifies collection subscribers. Handover uses the stone transition action.

## Manual and automatic billing

With `AUTO_BILL_AFTER_IDENTIFICATION=False`, billing is a separate action:
`GET /bills/preview/?order={id}` then `POST /bills/generate/` with an order ID.
The billing worklist selects active orders with all submitted stones registered.

With the flag enabled, registering the final stone triggers bill submission.
Handled pricing/submission failures are exposed through Billing needs attention.
`POST /bills/retry/` accepts an order ID and either creates its missing bill or
resubmits the existing failed bill. Accepted asynchronous submissions wait for a
control-number callback. See [bill submission](../gepg/01_BILL_SUBMISSION.md).

## Corrections

Category changes are allowed only in `received`, `on_hold`, or `cancelled` stone
states and never when its report is finalized. Exact type is a finding and must
match the category. Normal edits to a finalized report or its stone are refused.

`identification.edit_finalized_report` permits corrections. Updating a finalized
report refreshes the existing certificate's findings snapshot while retaining
its number, issuance date, issuer, and signatory names. See
[certificates](../engineering/certificates.md) for the rendering contract.

## Stone statuses and order stages

The usual service-driven path is:

```text
received -> billed -> paid -> certified -> ready_for_collection -> collected
```

`under_identification`, `on_hold`, and `cancelled` also exist. This is not an
enforced transition graph: `transition_stone()` accepts any target status,
skips a no-op, and records a `StatusHistory` row for a change. The API validates
the status choice and requires `orders.transition_stone`; it does not validate
the previous-to-next pair.

An order has no stored pipeline stage. `order_stage()` derives it from the
submitted count, registered stones, bill, and stone statuses. `orders_at_stage()`
expresses corresponding list filters in SQL; tests compare the two.

The separate `hold_status` is stored. Holding or cancelling an order overrides
its displayed stage without changing its stones or bill. `orders.hold_order`
allows hold/release; cancelling a fully paid order is refused. Releasing clears
the hold metadata. This does not implement a payment refund or GePG cancellation.
