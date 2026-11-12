# GePG flows in the current system

This guide is a **historical snapshot of the implementation reviewed on 2026-10-08**. Its gap descriptions and old acknowledgement/signing behavior were superseded by the protocol-alignment work. For current behavior, read [the adapter overview](00_GEPG_INTEGRATION_OVERVIEW.md), [bill submission](01_BILL_SUBMISSION.md), [payment notifications](02_PAYMENT_NOTIFICATION.md), and [production setup](04_GEPG_PRODUCTION_SETUP.md). Open the historical diagrams in a Markdown viewer with Mermaid support.

## Reading map

1. [End-to-end flow](#1-end-to-end-flow)
2. [Manual and automatic billing](#2-manual-and-automatic-billing)
3. [Local bill creation](#3-local-bill-creation)
4. [Submission and immediate responses](#4-submission-and-immediate-responses)
5. [Asynchronous control-number callback](#5-asynchronous-control-number-callback)
6. [Failed billing and retries](#6-failed-billing-and-retries)
7. [Payment notifications and duplicate handling](#7-payment-notifications-and-duplicate-handling)
8. [Payment and stone states](#8-payment-and-stone-states)
9. [Offline simulation](#9-offline-simulation)
10. [Signing and callback boundaries](#10-signing-and-callback-boundaries)
11. [Endpoint and source map](#11-endpoint-and-source-map)
12. [Unimplemented flows and important limits](#12-unimplemented-flows-and-important-limits)

## 1. End-to-end flow

The system creates the **bill number**, which identifies the local charge. GePG supplies the **control number**, which the customer uses to pay. A control number is not evidence of payment: the separate payment notification updates settlement.

```mermaid
flowchart TD
    A["Register customer order<br/>and identify stones"] --> B["Create one bill with<br/>one charge line per stone"]
    B --> C["Submit billSubReq XML<br/>to GePG"]
    C --> D{"GePG response"}
    D -->|"Control number returned<br/>immediately"| E["Save control number"]
    D -->|"Accepted acknowledgement"| F["Wait for billSubRes<br/>callback"]
    F --> E
    D -->|"Rejection or handled error"| R["Keep local bill and error details;<br/>see retry flow"]
    E --> G["Display control number<br/>to staff or customer"]
    G --> H["Customer pays through an<br/>external payment provider"]
    H --> I["GePG sends pmtSpNtfReq<br/>callback"]
    I --> J["Match bill and<br/>record payment"]
    J --> K{"Total received covers bill?"}
    K -->|No| L["Keep bill pending;<br/>stones remain billed"]
    L --> H
    K -->|Yes| M["Bill and stones paid;<br/>notify subscribers"]
    M --> N["Payment gate for findings<br/>is satisfied"]
```

Customer payment through a bank/mobile provider is external context. This repository does not initiate that transaction. Payment does not itself create findings or issue a certificate.

## 2. Manual and automatic billing

`AUTO_BILL_AFTER_IDENTIFICATION` selects the entry path; its settings default is `False`. This guide does not assert which value a deployed environment uses.

```mermaid
flowchart TD
    A["POST /api/v1/orders/{id}/stones/"] --> B["identify_stone:<br/>save the identified stone"]
    B --> C{"Automatic billing enabled?"}
    C -->|No| D["Return the stone; manual billing is available"]
    C -->|Yes| E{"Registered count equals<br/>submitted count?"}
    E -->|No| F["Return the stone; continue identification"]
    E -->|Yes| G{"Order held?"}
    G -->|Yes| H["Return billing_attention<br/>message"]
    G -->|No| I["generate_bill_for_order"]
    D --> J["GET /api/v1/bills/worklist/"]
    J --> K["GET /api/v1/bills/preview/<br/>?order={id}"]
    K --> L["Review category fees,<br/>total, and blockers"]
    L --> M["POST /api/v1/bills/generate/<br/>with order ID"]
    M --> I
    I --> N["Create local bill and<br/>submit to GePG"]
```

- The manual worklist selects orders with every submitted stone registered and no bill. Preview is read-only and reports an existing bill, an empty order, or missing category prices as blockers.
- The manual generation service itself checks for an existing bill, no stones, and missing prices; it does not repeat the worklist's complete-identification check or the automatic path's held-order check.
- Identification requires `orders.add_stone`. Automatic billing runs as part of that action without a separate `billing.generate_bill` check. Manual preview/generation and retry actions require `billing.generate_bill`.
- In automatic mode, manual generation and the manual worklist return 404. A handled pricing failure preserves identification and returns `billing_attention`; an unsuccessful submission can return a bill plus an attention message.

Sources: [identification service](../../apps/billing/services/identification.py), [API views](../../apps/billing/views.py), [worklist selectors](../../apps/billing/selectors.py).

## 3. Local bill creation

The charge is a fixed fee from each stone's **category**, not its weight or individual stone type. Bill items snapshot the price so future price changes do not rewrite an existing bill.

```mermaid
flowchart TD
    A["generate_bill_for_order"] --> B["Enter _create_local_bill<br/>atomic block"]
    B --> C{"Existing bill or no stones?"}
    C -->|Yes| X["Raise ServiceError;<br/>roll back local bill work"]
    C -->|No| D["Create pending Bill with<br/>order reference and expiry"]
    D --> E["Read next stone category price"]
    E --> F{"Price configured?"}
    F -->|No| X
    F -->|Yes| G["Create BillItem: fee, description,<br/>item reference; weight null"]
    G --> H["Transition stone to billed;<br/>write status history"]
    H --> I{"More stones?"}
    I -->|Yes| E
    I -->|No| J["Save total amount and leave<br/>local atomic block"]
    J --> K["_submit_existing_bill"]
```

The bill defaults to `TZS`; expiry is issuance plus `GEPG_BILL_EXPIRY_DAYS` (default 365). One bill belongs to one order, with multiple items and payments.

**Transaction boundary:** `ATOMIC_REQUESTS=True` wraps API views in a database transaction. Leaving `_create_local_bill()` is therefore not necessarily a database commit. The outbound HTTP request can happen before the enclosing request commits. A handled gateway failure returns normally, allowing the local bill to persist; an unhandled exception can roll back the request.

Sources: [bill service](../../apps/billing/services/bill.py), [bill models](../../apps/billing/models/bill.py), [database settings](../../config/settings/base.py).

## 4. Submission and immediate responses

```mermaid
sequenceDiagram
    participant S as Bill service
    participant A as GePG adapter
    participant G as GePG
    participant DB as Database
    S->>A: submit_bill(bill, customer,<br/>username)
    alt GEPG_SIMULATE is true
        A-->>S: Generated control number (code 7101)
    else Real submission
        A->>A: Build billSubReq and sign if enabled
        A->>G: POST GEPG_BILL_CREATE_URL<br/>(30-second timeout)
        alt billSubRes contains a control number
            G-->>A: BillCntrNum and status fields
            A-->>S: Synchronous result with control number
        else billSubReqAck code is 7101 or 7241
            G-->>A: Accepted acknowledgement
            A-->>S: Success with internal PENDING marker
        else Rejection or handled transport / XML error
            G-->>A: Rejection, invalid response, or HTTP failure
            A-->>S: Failure result with diagnostic code
        end
    end
    S->>DB: Save attempt timestamp,<br/>status_code, status_desc
    opt Successful result has a real control number
        S->>DB: Save control_number
    end
    Note over S,DB: Bill payment status<br/>remains pending
```

Connection failures and timeouts also take the handled transport-error branch, even if GePG sends no response.

The XML contains `BillHdr`, `BillDtls/BillDtl`, and item details. Amounts have two decimal places; customer text is escaped and phone numbers normalized. Current fixed values include `BillPayOpt=3`, `MinPayAmt=full bill amount`, and `ExchRate=1.00`. Provider identifiers come from settings, even when a `ServiceProvider` record is attached to the bill.

HTTP headers are `Content-Type: application/xml`, `Gepg-Com: default.sp.in`, and `Gepg-Code: GEPG_SP_CODE`.

| Result | Local behavior |
| --- | --- |
| Synchronous `billSubRes` with nonempty `BillCntrNum` | Treat as success based on number presence; store number and raw status fields |
| `billSubReqAck` with `7101` or `7241` | Keep control number empty and wait for callback; internal `PENDING` is not saved as the number |
| Other acknowledgement code | Store rejection details |
| Request/HTTP exception | Store `CONNECTION_ERROR` |
| Caught XML parsing failure | Store `XML_PARSE_ERROR` |
| Unrecognized response structure | Store `UNKNOWN_RESPONSE` |

`is_gepg_submitted=True` means an attempt returned a result, including a failure. It does not prove acceptance. The adapter returns `raw_response` and `is_sync`, but the submission service does not persist them.

Source: [gateway adapter](../../apps/billing/gateways/gepg.py), [submission service](../../apps/billing/services/bill.py).

## 5. Asynchronous control-number callback

```mermaid
sequenceDiagram
    participant G as GePG
    participant W as BillResponseView
    participant S as Bill callback service
    participant DB as Database
    participant UI as Manual generation dialog
    participant API as Bill read API
    G->>W: POST /gepg/bill/response/<br/>with billSubRes
    W->>S: Decode UTF-8 body and handle callback
    S->>S: Parse ResId, BillId,<br/>BillCntrNum, status fields
    alt Caught parsing failure
        S-->>W: billSubResAck with ERROR and 7102
    else Parse succeeds
        S->>DB: Find Bill by bill_number = BillId
        opt Matching bill and nonempty control number
            S->>DB: Save control number and gateway status fields
        end
        S-->>W: billSubResAck with ResId and 7101
    end
    W-->>G: HTTP 200 with<br/>acknowledgement XML
    loop While dialog query is active and number is missing
        UI->>API: GET /api/v1/bills/{id}/<br/>every 3 seconds
        API->>DB: Read current bill
        DB-->>API: Bill fields
        API-->>UI: Serialized bill including control_number
    end
```

Polling reads local state and does not ask GePG for a new number. The manual dialog stops its polling interval once a number appears.

A parseable callback for an unknown bill, or one without a control number, still gets `7101` without updating a bill. A supplied number is saved without checking that `BillStsCode` indicates success. Only the first matching `BillDtl` is parsed. These are current implementation behaviors, not recommended protocol guarantees.

Sources: [callback service](../../apps/billing/services/bill.py), [webhooks](../../apps/billing/webhooks.py), [frontend dialog](https://github.com/TGCTZ/TGC-SERVICE-FRONTEND/blob/main/src/features/orders/components/generate-bill-dialog.tsx).

## 6. Failed billing and retries

Retry is an explicit staff action exposed in automatic mode. There is no scheduled retry worker.

```mermaid
flowchart TD
    A["GET /api/v1/bills/attention/"] --> B["Staff reviews failure<br/>and corrects its cause"]
    B --> C["POST /api/v1/bills/retry/<br/>with order ID"]
    C --> D{"Order held?"}
    D -->|Yes| E["Refuse: release order first"]
    D -->|No| F{"Local bill exists?"}
    F -->|No| G{"Nonzero submitted count<br/>and all stones identified?"}
    G -->|No| H["Refuse: identify<br/>every stone"]
    G -->|Yes| I["Create bill, then submit"]
    F -->|Yes| J{"pending + no control number + code not 7101/7241?"}
    J -->|Yes| K["Resubmit existing bill<br/>with same number and lines"]
    J -->|No| L["Refuse retry:<br/>bill already submitted"]
    K --> M["Process submission result<br/>as in section 4"]
    I --> M
```

The attention worklist selects active, fully identified orders with either no bill or a bill matching the retry condition. Retrying an existing bill preserves its prices and stone statuses. Both attention and retry endpoints return 404 in manual mode.

An accepted asynchronous bill with no control number is **not** a retry candidate. If its callback never arrives, the current implementation has no automatic recovery flow.

Sources: [retry service](../../apps/billing/services/bill.py), [attention selector](../../apps/billing/selectors.py), [API mode checks](../../apps/billing/views.py).

## 7. Payment notifications and duplicate handling

An acknowledgement confirms callback handling. The bill's status separately indicates whether enough money has been received.

```mermaid
flowchart TD
    A["POST /gepg/payments/notification/<br/>with pmtSpNtfReq"] --> B["Parse PmtHdr and all<br/>PmtTrxDtl entries"]
    B -->|"Caught parse/value error"| X["Return pmtSpNtfReqAck<br/>with code 7102"]
    B -->|Success| C["Apply next transaction<br/>in its own atomic block"]
    C --> D["Find bill by BillId; fall back to BillCtrNum"]
    D --> E{"Bill found?"}
    E -->|No| X
    E -->|Yes| F["get_or_create Payment by TrxId;<br/>retain fields and raw XML"]
    F --> G{"New payment?"}
    G -->|No| H["Update existing payment;<br/>skip settlement recalculation"]
    G -->|Yes| I["Sum this bill's live payment amounts"]
    I --> J{"Sum at least bill total?"}
    J -->|No| K["Keep bill pending"]
    J -->|Yes| L["Set bill paid; transition stones<br/>to paid; notify subscribers"]
    H --> M{"More transactions?"}
    K --> M
    L --> M
    M -->|Yes| C
    M -->|No| N["Return pmtSpNtfReqAck<br/>with code 7101"]
    C -.->|"Caught application error;<br/>roll back this entry"| X
```

**Identical redelivery:** the same `TrxId` updates the existing row and skips settlement, avoiding another payment and repeated settlement side effects. The database uniqueness constraint applies to nonempty transaction IDs on live rows. The parser does not require a nonempty ID, so this guarantee should not be generalized to malformed transactions.

**Changed redelivery:** a different amount under an existing `TrxId` overwrites the stored payment but does not recalculate bill status. This is not a correction/reconciliation workflow.

**Multiple entries:** parsing happens before application. Once application starts, an earlier successful entry can survive a later entry's failure and the resulting `7102`. Each application has an atomic block; the loop does not guarantee all-or-nothing processing of the whole notification. With `ATOMIC_REQUESTS`, successful earlier work can commit when the view returns the handled failure acknowledgement normally.

Handled responses use HTTP 200 and `text/xml; charset=utf-8`; the XML code carries success (`7101`) or failure (`7102`). Uncaught exceptions can still produce a server error instead of an acknowledgement, for example invalid decimal amounts during parsing.

Sources: [payment service](../../apps/billing/services/payment.py), [parser and acknowledgement builder](../../apps/billing/gateways/gepg.py), [payment model](../../apps/billing/models/bill.py).

## 8. Payment and stone states

```mermaid
stateDiagram-v2
    [*] --> pending: Local bill created; stones become billed
    pending --> pending: Submission result or control-number callback
    pending --> pending: New payment; cumulative amount below total
    pending --> paid: New payment; cumulative amount covers total
    paid --> paid: Additional payment; cumulative amount still covers total
```

This diagram assumes ordinary positive payments; inbound amount validation is limited. Duplicate transaction IDs do not take these settlement transitions.

| Event | Bill state | Stone effect |
| --- | --- | --- |
| Local bill created | `pending` | Transition to `billed` and record history |
| Submission fails or waits for callback | Remains `pending` | Remain `billed` |
| Control number received | Remains `pending` | No payment transition |
| New payment leaves cumulative sum below total | `pending` | No stone transition |
| New payment brings cumulative sum to or above total | `paid` | Transition all order stones to `paid`; notify bill-paid subscribers |
| Same transaction redelivered | No recalculation | No transition |

`transition_stone()` does nothing when the stone is already in the target state. New transactions reaching the full-payment branch can still invoke notifications again. A cancellation callback transitions a bill to `cancelled` only when GePG returns `7283`; `expired` has no local worker or automatic state transition.

Sources: [payment service](../../apps/billing/services/payment.py), [stone transitions](../../apps/orders/services/stone.py), [status definitions](../../apps/gems/enums.py).

## 9. Offline simulation

There are two separate switches/paths: simulated **submission** bypasses GePG HTTP, while simulated **payment** exercises the real local callback handler.

```mermaid
flowchart TD
    A["Submit bill with<br/>GEPG_SIMULATE=True"] --> B["Skip XML signing<br/>and outbound HTTP"]
    B --> C["Return generated 12-digit<br/>number beginning 99<br/>(code 7101)"]
    D["POST /api/v1/bills/{id}/<br/>simulate-payment/"] --> E{"DEBUG and GEPG_SIMULATE<br/>enabled?"}
    E -->|No| F["404 for an otherwise authorized request"]
    E -->|Yes| G["Validate optional positive amount"]
    G --> H["Assign fake control number<br/>if missing"]
    H --> I["Use supplied amount or<br/>remaining balance"]
    I --> J["Build fake pmtSpNtfReq<br/>with fresh SIM transaction ID"]
    J --> K["Call process_payment_notification<br/>directly"]
    K --> L["Run normal recording<br/>and settlement flow"]
    L --> M["Refresh and return bill"]
```

The payment API action also requires `billing.generate_bill`. It always simulates payment of the outstanding balance in full. Each simulation creates a fresh transaction ID. The helper calls the service directly rather than making an HTTP request to the webhook, and it does not inspect the returned acknowledgement before returning the refreshed bill.

The outbound `GEPG_SIMULATE` check alone does not require `DEBUG`. The frontend reads simulation availability from `/api/v1/config/`.

Sources: [simulation helper](../../apps/billing/dev.py), [API action](../../apps/billing/views.py), [configuration endpoint](../../apps/core/views.py).

## 10. Signing and callback boundaries

```mermaid
flowchart TD
    A["Outbound bill XML or<br/>callback acknowledgement XML"] --> B{"Digital signature<br/>enabled?"}
    B -->|No| C["Return XML with signature<br/>placeholder unchanged"]
    B -->|Yes| D["Load cached private key<br/>from PKCS#12 file"]
    D --> E["Sign inner Gepg content<br/>excluding signature element"]
    E --> F["RSA PKCS1v15 + SHA-256;<br/>Base64 encode"]
    F --> G["Replace signature placeholder"]
    D -->|"PFX load or signing error"| H["Raise error and stop outbound request"]
    I["Inbound GePG callback"] --> J["CSRF-exempt Django POST view;<br/>no JWT check"]
    J --> K["Parse XML with defusedxml"]
    K --> L["Run bill or payment handler<br/>without signature verification"]
```

The signing helper serves submissions and callback acknowledgements. When enabled, a PFX load or signing error stops the outbound request instead of returning placeholder XML. `GEPG_PUBLIC_CERT_PATH` is configured but unused: safe XML parsing does not authenticate the sender, and a forged matching payment notification can affect settlement.

Sources: [signing](../../apps/billing/gateways/signing.py), [webhooks](../../apps/billing/webhooks.py).

## 11. Endpoint and source map

Application endpoints are under `/api/v1/`; callbacks are deliberately outside that prefix so their registered URLs remain stable across API versions.

| Endpoint | Role / availability |
| --- | --- |
| `POST /api/v1/orders/{id}/stones/` | Identify a stone; may trigger automatic billing |
| `GET /api/v1/bills/worklist/` | Fully identified, unbilled orders; manual mode |
| `GET /api/v1/bills/preview/?order={id}` | Read-only charge preview |
| `POST /api/v1/bills/generate/` | Create and submit bill; manual mode |
| `GET /api/v1/bills/attention/` | Automatic billing problems; automatic mode |
| `POST /api/v1/bills/retry/` | Create missing bill or resubmit failed bill; automatic mode |
| `GET /api/v1/bills/` and `GET /api/v1/bills/{id}/` | Read bills, amounts, control numbers, and gateway status |
| `GET /api/v1/bills/workflow-feed/` | Existing unpaid bills and relevant pending orders |
| `GET /api/v1/bill-items/` and `GET /api/v1/payments/` | Read charge lines and payment records |
| `POST /api/v1/bills/{id}/simulate-payment/` | Development payment simulation |
| `POST /api/v1/bills/{id}/cancel/` | Permission-checked bill cancellation request |
| `POST /api/v1/reconciliations/request/` | Permission-checked reconciliation request for a transaction date |
| `POST /gepg/bill/response/` | `billSubRes` in; `billSubResAck` out |
| `POST /gepg/payments/notification/` | `pmtSpNtfReq` in; `pmtSpNtfReqAck` out |
| `POST /billing/api/payments/notification/` | Legacy registered payment callback path |
| `POST /billing/api/bill/response/` | Legacy registered bill-response callback path |
| `POST /billing/api/bill/cancel-response/` | Legacy registered cancellation callback path |
| `POST /billing/reconciliation/response/` | Legacy registered reconciliation callback path |
| `POST <GEPG_BILL_CREATE_URL>` | Outbound submission from backend to GePG |

A control-number SMS is attempted through Beem when a number is received synchronously or by callback. Successful sends are recorded on the bill to prevent repeat sends.

Bill/item/payment CRUD is read-only to API clients; the named actions and callback services perform the writes. Service-provider CRUD exists separately at `/api/v1/service-providers/`, but outbound XML currently uses settings for its provider codes.

| Source | Follow it to understand |
| --- | --- |
| [Root URLs](../../config/urls.py), [billing URLs](../../apps/billing/urls.py), [callback URLs](../../apps/billing/urls_webhooks.py) | Routing and callback isolation |
| [Identification service](../../apps/billing/services/identification.py) | Automatic handoff after the last stone |
| [Bill service](../../apps/billing/services/bill.py) | Preview, local creation, submission tracking, retries, control-number callback |
| [GePG operations service](../../apps/billing/services/gepg_operations.py) | Bill cancellation and reconciliation requests/callbacks |
| [Gateway adapter](../../apps/billing/gateways/gepg.py) | Exact XML fields, HTTP transport, parsing, acknowledgement codes |
| [Payment service](../../apps/billing/services/payment.py) | Matching, duplicate handling, full settlement |
| [Models](../../apps/billing/models/bill.py), [serializers](../../apps/billing/serializers.py) | Stored records and exposed API fields |
| [Billing tests](../../apps/billing/tests/test_billing.py), [automatic billing tests](../../apps/billing/tests/test_auto_billing.py) | Existing examples for settlement, redelivery, simulation, and retries |
| [API examples](../../api.http) | Sample requests and callback XML |

## 12. Unimplemented flows and important limits

These limits explain why a flow might stop or behave differently from a full GePG integration:

- **Cancellation and reconciliation verification:** cancellation and reconciliation request/callback handlers and API actions are implemented. The old cancellation code described its destination as a mock, and neither workflow has been verified against live GePG. Refunds are not implemented.
- **SMS delivery:** Beem credentials are required. Failed or ambiguous provider delivery is not guaranteed to be exactly once.
- **Expiry:** `expiry_at` is stored and sent, but there is no local expiry worker or automatic bill-state transition.
- **Missing callbacks:** there is no scheduled retry/reconciliation process. Accepted asynchronous submissions remain waiting and cannot use the current retry action.
- **Authentication:** inbound signatures are not verified. With signing enabled, missing or invalid signing material stops the outbound request.
- **Settlement validation:** payment amounts are summed without currency conversion or an `is_processed` filter. The callback does not verify currency agreement, positive payment amounts, or whether the order is held before settlement.
- **Corrections and ordering:** changed duplicate transactions do not recalculate settlement. A fresh payment that reaches the full-payment branch transitions every order stone to `paid`, without a guard against a later workflow state.
- **Concurrency and early callbacks:** submission can run before the request transaction commits, and settlement does not lock the bill while summing payments. The current flow should not be read as a guarantee against early-callback or simultaneous-payment races.
- **Acknowledgement coverage:** unknown bill and payment callbacks receive `7102`; payment processing can partly succeed before a failure acknowledgement. Parser, signing, and database failures still require operational monitoring.

For VPS configuration and cutover steps, see the [production setup](04_GEPG_PRODUCTION_SETUP.md). For protocol configuration, see the [adapter overview](00_GEPG_INTEGRATION_OVERVIEW.md); focused explanations are in [bill submission](01_BILL_SUBMISSION.md) and [payment notifications](02_PAYMENT_NOTIFICATION.md).
