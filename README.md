# TGPMIS API

The Tanzania Gemmological Centre's stone-certification system, as a REST API.

A customer brings stones in; a gemmologist registers each stone's pricing
category; the order is billed through the GePG payment gateway; once settled,
the gemmologist records findings and finalizes a report that issues a printable
PDF certificate.

```
received -> under identification -> billed -> paid -> certified
         -> ready for collection -> collected
```

Category registration comes *before* billing because the category determines
the flat fee. Findings and certificate issuance follow payment. Pending work is
shown in the relevant frontend resource pages; stone transitions do not follow
an enforced status graph.

Set `AUTO_BILL_AFTER_IDENTIFICATION=True` in `.env` to submit the bill to GePG
as soon as the last submitted stone is registered. The default `False` keeps
bill generation as a separate action. In automatic mode, missing prices or
failed submissions appear in **Billing needs attention**; retrying resubmits the
same bill when one already exists. An accepted asynchronous GePG response may
deliver its control number later through the callback.

The React client lives in the
[TGC-SERVICE-FRONTEND](https://github.com/TGCTZ/TGC-SERVICE-FRONTEND) repository.

## Stack

| Concern | Choice |
|---|---|
| Runtime | Python 3.13, Django 5.2 LTS |
| API | Django REST Framework |
| Auth | `djangorestframework-simplejwt` (access + refresh, blacklist on logout) |
| RBAC | Django `Group` + `Permission`, defined in code |
| Database | PostgreSQL (SQLite in tests) |
| Dependencies | `uv` with a committed lockfile |
| Docs | `drf-spectacular` (OpenAPI 3, Swagger UI, ReDoc) |
| Tests | `pytest`, `pytest-django`, `factory_boy` |
| Quality | `ruff` (lint + format + import sort), `pre-commit` |

## Quick start

Certificate PDFs are rendered by WeasyPrint, which needs native system libraries.
Install them before using certificate PDF downloads. The renderer is loaded on
demand, so other API endpoints and management commands can start without them.
On Debian/Ubuntu:

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  libcairo2 libgdk-pixbuf-2.0-0 libffi8 shared-mime-info
```

```bash
uv sync
cp .env.example .env
uv run python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
# paste the result into SECRET_KEY in .env, then create the database:
createdb -U postgres tgcservice

uv run python manage.py migrate
uv run python manage.py seed
uv run python manage.py runserver
```

Then open <http://localhost:8000/api/schema/swagger-ui/>.

Health probes sit outside the versioned API, so they survive a version bump:

| Endpoint | Purpose |
|---|---|
| `GET /api/health/` | Liveness. Touches nothing external; 200 while the process runs. |
| `GET /api/health/ready/` | Readiness. Checks the database and pending migrations; 503 if either fails. |

Demo accounts are `<role>@example.com` with the password printed by `seed`, for each
of `superadmin`, `admin`, `manager`, `receptionist`, `gemmologist` and `accountant`.
`seed_reference_data` creates shared lookup rows, roles, and permissions without
creating demo accounts or workflow records; `seed` runs it before adding demo data.
Use `seed_reference_data` by itself when setting up an environment without demo data.
For reports to have useful history to show, seed a fresh
database with history instead:
`uv run python manage.py seed --orders 150 --history-months 12`.

Outside the demo data, accounts are created from the Users screen by an email and
a role - there is no sign-up. The API returns a temporary password once to the
creator, who shares it with the new user manually; that user must set their own
password and complete their profile on first sign-in. See
[Accounts and first login](docs/engineering/accounts.md).

## Everyday commands

```bash
uv run pytest                       # run the suite
uv run pytest --cov                 # with coverage
uv run ruff check . && uv run ruff format .
uv run pre-commit install           # once per clone
uv run python scripts/check_docs.py # documentation links still resolve
uv run python manage.py setup_roles # re-apply the role matrix after editing roles.py
uv run python manage.py setup_roles --prune  # also drop roles the matrix no longer declares
```

## Production deployment

For the VPS setup, GitHub environment values, and automatic release flow, see
the [backend deployment guide](docs/deployment.md). The commands below are a
manual deployment reference.

The backend GitHub Actions workflow deploys a tested commit pushed to `main`.
It checks out that commit on the production host, installs the locked runtime
dependencies, applies migrations, collects static files, restarts the service,
and checks readiness. To run those deployment steps manually, first check out
the intended commit on the production host, then set these values for your
server and domain:

```bash
TGC_BACKEND_DIR=/path/to/backend
TGC_DJANGO_SERVICE=tgpmis-gunicorn.service
TGC_API_BASE_URL=https://api.example.com

cd "$TGC_BACKEND_DIR"
export DJANGO_SETTINGS_MODULE=config.settings.production
uv sync --frozen --no-dev
uv run python manage.py migrate --noinput
uv run python manage.py collectstatic --noinput
sudo systemctl restart "$TGC_DJANGO_SERVICE"
curl --fail --silent --show-error "${TGC_API_BASE_URL%/}/api/health/ready/"
```

For a new production database, run these once after configuring the production
environment and applying migrations:

```bash
TGC_BACKEND_DIR=/path/to/backend
cd "$TGC_BACKEND_DIR"
export DJANGO_SETTINGS_MODULE=config.settings.production
uv run python manage.py seed_reference_data
uv run python manage.py createsuperuser
```

Do not run `seed` in production; it creates demo accounts and workflow records.
`seed_reference_data` synchronizes declared roles as well as lookup data, so see
the [operations guide](docs/engineering/operations.md#role-synchronization-resets-declared-roles)
before rerunning it on an established system.

## Documentation

New to the codebase? Read them in this order — what the system does first, then
how it is built.

| Document | What it covers |
|---|---|
| [Business workflow](docs/domain/business-workflow.md) | The stone's journey: stages, roles, status lifecycle |
| [Business decisions](docs/domain/decisions.md) | The rules behind the workflow - settled, provisional, still open |
| [Project structure](docs/engineering/project-structure.md) | Every app and layer, and what belongs in each |
| [Diagrams](docs/diagrams/README.md) | Request flows, drawn — start here if you prefer pictures |
| [API lifecycle](docs/engineering/api-lifecycle.md) | A request traced from URL to response |
| [Testing the API](docs/engineering/testing-the-api.md) | Swagger, `api.http`, the query contract, the test suite |
| [Permissions](docs/engineering/permissions.md) | Roles, the hierarchy, permissions, module gates, workflow verbs |
| [Accounts and first login](docs/engineering/accounts.md) | Staff-created accounts, manual credential sharing, first login, password resets |
| [GePG integration](docs/gepg/README.md) | The payment gateway, and the gaps in it |
| [Certificates](docs/engineering/certificates.md) | Snapshots, the PDF, QR verification and report numbering |
| [Reports](docs/engineering/reports.md) | Financial and operational sections, filters, and exports |
| [Reference data](docs/domain/reference-data.md) | The lookup lists, and Tanzania's regions |
| [Operations](docs/engineering/operations.md) | Configuration, runtime setup, and what to run after a deploy |
| [Conventions](docs/engineering/conventions.md) | The numbered rules this project holds itself to |
| [Adding an app](docs/engineering/adding-an-app.md) | The shape every domain app follows |
| [Tech stack](docs/engineering/tech-stack.md) | Runtime components, project-specific choices, and engineering tools |

## Layout

```
config/            settings package (base/development/production/test), root urls
apps/
  core/            L1 - base models, managers, shared DRF machinery
  users/           L2 - custom user, authentication, RBAC
  audit/           L2 - activity-log and system-log read APIs
  notifications/   L2 - per-user in-app notifications
  gems/            L2 - domain enums and the stone reference tables
  orders/          L3 - customers, orders, stones, status trail
  billing/         L4 - bills, payments, the GePG gateway
  identification/  L4 - full gemmological identification
  certificates/    L5 - certificates and their PDF documents
  reports/         L6 - read-only financial and operational reports
docs/              engineering, domain and GePG documentation
api.http           a runnable request collection for the whole pipeline
```

Apps are grouped by dependency layer. Prefer dependencies on lower layers and
keep same-layer apps independent where practical. Existing code has exceptions,
including identification using certificate services.
