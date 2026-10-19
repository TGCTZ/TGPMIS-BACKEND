# Adding a domain app

The five domain apps were all built to the same shape. This is that shape,
written down, so the sixth one looks like the first five.

Before starting, check the work belongs in a new app at all. A new *table* in an
existing app is usually right; a new app is for a new **stage of the pipeline**
with its own services and its own permissions.

## 1. Pick the layer first

Prefer dependencies on lower layers. Keep same-layer apps independent where
practical, but inspect the current import graph before treating the layer order
as a strict rule. Identification currently calls certificate services when it
finalizes a report and refreshes certificate findings.

| Layer | App | Holds |
|---|---|---|
| L1 | `apps.core` | Base models, managers, shared DRF machinery |
| L2 | `apps.users` | Custom user, authentication, RBAC |
| L2 | `apps.audit` | Read-only APIs over the activity log and log file |
| L2 | `apps.gems` | Every domain enum, and the stone reference tables |
| L3 | `apps.orders` | Customers, orders, stones, status trail |
| L4 | `apps.billing` | Bills, payments, the GePG gateway |
| L4 | `apps.identification` | Full gemmological identification |
| L5 | `apps.certificates` | Certificates and their PDF documents |

`identification` gates findings on the bill being paid without importing
`billing`: it uses ORM traversal (`stone.order.bill`) and the enum from `gems`.
The report finalization flow does depend on certificate services. Treat the
layer map as guidance for new dependencies, and update it when the import graph
changes.

## 2. Scaffold

```bash
uv run python manage.py startapp <name> apps/<name>
```

Set `name = "apps.<name>"` in the `AppConfig`, and put the layer in its
docstring:

```python
class BillingConfig(AppConfig):
    """L4 - bills, payments and the GePG payment gateway."""
```

Add it to `LOCAL_APPS` in `config/settings/base.py`, in layer order, with the
same one-line annotation.

## 3. Models

Everything inherits `BaseModel` - soft delete, audit columns, auditlog
registration, all free. `ReferenceModel` for a lookup table.

Two exceptions are allowed, and both must justify themselves in the docstring:
an **append-only ledger** (`StatusHistory`) is a plain
`models.Model`, because a soft-deletable audit trail is a contradiction and
logging the log is circular.

Natural keys get a partial unique constraint, never `unique=True`:

```python
models.UniqueConstraint(
    fields=["reference_number"],
    condition=Q(deleted_at__isnull=True),
    name="%(app_label)s_%(class)s_unique_reference_number",
)
```

Reference numbers come from `apps/core/services.py`:

```python
generate_order_reference_number()  # ORD-2627-1
reference_number_for_order(order, "BILL")  # BILL-2627-1
reference_number_for_order(order, "TGC", stone_label="1")  # TGC-2627-1-1
```

For orders, this allocates the next natural-number sequence for the current
financial year from the locked `OrderSequence` counter. The counter is first
initialized from all existing order references, including soft-deleted orders.
Related bills, findings, and certificates derive their sequence from the order with
`reference_number_for_order(order, prefix, stone_label=...)`; they do not have
independent counters. Existing padded references remain valid and unchanged.
`format_reference_number()` adds the financial-year slash to human-facing values
while stored and gateway IDs stay slash-free.

References share an order sequence and use a prefix to identify the document;
per-stone report and certificate numbers add the stone label. New orders label
stones `1`, `2`, …; an order with existing letter labels continues `A`, `B`, …,
then `AA`, `AB`, …. The year pair is the **financial** year — July to June,
each year written as two digits and answered by `financial_year()`. The order
sequence restarts with it. See [certificates.md](certificates.md).

## 4. Services

The business rules go here, and nowhere else. Module-level functions,
keyword-only after the subject, `@transaction.atomic` for multi-row writes,
raising `ServiceError` - which `apps/core/handlers.py` turns into a 400 with the
message intact. **No view needs a try/except.**

Keep an explicit `user=` parameter even though `CurrentUserMiddleware` exists.
The contextvar stamps `created_by`/`updated_by` from a *request*; who finalized a
report or settled a bill is a business fact, and these services are also called
from a management command and from inside a webhook where there is no request
user at all.

## 5. Selectors

A queryset that answers "what is waiting to be worked on" goes in
`selectors.py`, not in a view - convention 21.

## 6. Serializers

Relations are exposed twice: a writable id and a read-only `*_detail` object.

```python
stone_type_detail = StoneTypeSerializer(source="stone_type", read_only=True)
```

Anything the service owns is **read-only**: reference numbers, tokens, snapshots,
and any status that moves through a workflow action. A writable `status` field
would let a client change state without leaving a trail.

## 7. Views

Thin. Permissions, queryset, delegate.

```python
class BillViewSet(BaseModelViewSet, viewsets.ModelViewSet):
    queryset = Bill.objects.select_related("order", "order__customer")
    serializer_class = BillSerializer

    search_fields = ...
    filter_fields = ...
    ordering_fields = ...
    date_filter_fields = ...

    action_permissions = {"generate": ["billing.generate_bill"]}
```

Declare all four whitelists - an un-whitelisted parameter is silently ignored,
so a missing entry looks like a filter that does not work.

Workflow verbs are actions gated by `ActionPermissions` - convention 22.

## 8. Roles

Add the models to `apps/users/roles.py` and re-apply:

```bash
uv run python manage.py setup_roles
```

Then **check it from the outside**, because a permission granted to nobody fails
silently until someone opens the screen:

```bash
# in api.http, change the sign-in email to each role in turn
```

## 9. Tests

Factories in `tests/factories.py` support tests; selected factories are also
used for generated demo records. Add canonical lookup rows to
`manage.py seed_reference_data` when an app needs environment-wide reference
data. Module-level `pytestmark = pytest.mark.django_db`, plain functions named
for the behaviour.

Every list endpoint gets a query-count test - and it must create its rows inside
`set_current_user(...)`, or the audit-label N+1 stays invisible (convention 23).

## 10. Before committing

```bash
uv run pytest
uv run python manage.py makemigrations --check --dry-run
uv run ruff check . && uv run ruff format --check .
```

Then add the new endpoints to [`api.http`](../../api.http) and walk them once -
see [testing-the-api.md](testing-the-api.md).
