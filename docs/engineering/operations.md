# Operations

How the API is configured and what to run when it is deployed. Local setup is in
the [README](../../README.md#quick-start).

## Settings modules

| Entry point | Settings module |
| --- | --- |
| `manage.py` | `config.settings.development` |
| `config/wsgi.py`, `config/asgi.py` | `config.settings.production` |
| `pytest` | `config.settings.test` |

Production forces `DEBUG = False`, redirects to HTTPS, sends HSTS, marks cookies
secure and serves JSON only (no browsable API). It expects to sit behind a proxy
that sets `X-Forwarded-Proto`.

## Configuration

Every value is read from the environment (or `.env`) in
`config/settings/base.py`. `.env.example` lists them all; a test fails if a
setting reads a variable that file does not mention.

| Variable | Default | Notes |
| --- | --- | --- |
| `SECRET_KEY` | - | **Required.** No fallback, so a missing key stops the app. |
| `DEBUG` | `False` | Never true in production. |
| `ALLOWED_HOSTS` | - | Comma-separated. Required once `DEBUG` is off. |
| `DATABASE_URL` | - | **Required.** `postgres://user:password@host:5432/name`. |
| `CORS_ALLOWED_ORIGINS` | - | The frontend's origin(s), comma-separated. |
| `TIME_ZONE` | `UTC` | Storage and API timestamps. Leave at UTC. |
| `LAB_TIME_ZONE` | `Africa/Dar_es_Salaam` | The lab's calendar: which day a statistic counts an event on. |
| `LOG_LEVEL` | `INFO` | For `logs/app.log`, which the System Logs screen reads. |
| `JWT_ACCESS_MINUTES` | `60` | Access-token lifetime; logout or session inactivity can revoke access sooner. |
| `JWT_REFRESH_DAYS` | `14` | How long a user stays signed in without re-entering a password. |
| `FRONTEND_URL` | `http://localhost:5173` | Configured frontend base URL; no current account-email flow uses it. |
| `EMAIL_URL` | `consolemail://` | Django email backend configuration, e.g. `smtp+tls://user:password@host:587`. The current application does not send email; the default backend would write messages to the log if a mail call is added. |
| `DEFAULT_FROM_EMAIL` | `Tanzania Gemmological Centre <no-reply@tgc.go.tz>` | Configured Django sender address. No current application mail flow uses it. |
| `CERTIFICATE_LAB_NAME` | `Tanzania Gemmological Centre` | Used on the verify page and in the certificate's text header if the banner image is missing. |
| `CERTIFICATE_LAB_ADDRESS` | empty | Printed on the certificate. |
| `CERTIFICATE_MINISTRY_NAME` | `Ministry of Minerals` | The certificate's text header, if the banner image is missing. |
| `CERTIFICATE_VERIFY_BASE_URL` | `http://localhost:8000` | Absolute base of the QR link on every certificate. Must be publicly reachable. |
| `GEPG_SIMULATE` | `False` | Fake control numbers and payments, to walk the pipeline offline. Needs `DEBUG` too. |
| `GEPG_*_URL`, `GEPG_*_CODE` | empty | Gateway endpoints and the lab's codes - see [GePG integration](../gepg/README.md). |
| `GEPG_COM` | empty | Required registered `Gepg-Com` header in production. |
| `GEPG_ALG` | empty | Optional `Gepg-Alg` header; omitted when unset. |
| `GEPG_USE_DIGITAL_SIGNATURE` | `False` | Sign payloads with the PKCS#12 key below; required in production. |
| `GEPG_VERIFY_CALLBACK_SIGNATURE` | `False` | Match legacy callback handling; public-certificate verification is opt-in. |
| `GEPG_CERTIFICATE_PASSWORD` | empty | The key's passphrase. |
| `GEPG_PRIVATE_KEY_PATH` | `certificates/private.pfx` | Required production signing key. |
| `GEPG_PUBLIC_CERT_PATH`, `GEPG_PUBLIC_CERTIFICATE_PASSWORD` | `certificates/public.pfx`, empty | Used only when callback signature verification is enabled. |
| `GEPG_BILL_EXPIRY_DAYS` | `365` | How long a control number stays payable. |

## Before the first deploy

WeasyPrint loads native system libraries when the certificate PDF renderer is
first used. Other API endpoints can start without them, but certificate downloads
require them. See the
[README](../../README.md#quick-start) for the package list.

Account creation does not send credentials by email. The creator must share the
one-time temporary password shown on screen with the intended user.

## Deploying

Every deploy:

```bash
uv sync --frozen
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput   # the Django admin's assets
```

The first deploy only, after `migrate`:

```bash
uv run python manage.py seed_reference_data  # shared lookups, roles and permissions
uv run python manage.py createsuperuser   # the first account; it can create the rest
```

A Django superuser ranks at the top of the role hierarchy, so it can create
admins and managers from the Users screen. Do not run `seed` in production - it
creates demo accounts with a known password.

`seed_reference_data` is safe to rerun for missing reference rows and preserves
existing lookup values, including prices. Colors and instruments are the
exceptions: the identification palette is authoritative, and only the canonical
instrument list stays active. Other color rows are soft-deleted; other
instruments are deactivated so report history remains intact. The command also
runs `setup_roles`, which replaces permissions on declared roles with the matrix
in `apps/users/roles.py`. Role edits made in the UI for those roles will
therefore be overwritten; use `sync_colors` or `sync_instruments` when only
those lists need updating.

To synchronize the identification color palette in development or production,
run this command against that environment's database after deploying the code
and applying migrations:

```bash
uv run python manage.py sync_colors
```

This color-specific command restores the canonical palette, updates its groups,
and soft-deletes colors outside the palette without synchronizing role permissions.

To synchronize the active instrument list in development or production, run this
command against that environment's database after deploying the code:

```bash
uv run python manage.py sync_instruments
```

This command activates the ten canonical instruments and deactivates every
other live instrument without deleting rows or report usage. Development's
`seed` command runs the same synchronization through `seed_reference_data`.

Species, variety, origin, shape/cut, and treatment lookups start empty. The
rollout migration permanently removes their current rows and clears those
fields on reports; issued certificates retain their frozen snapshots. The
development demo seeder also leaves these findings blank. Users can add values
from the searchable findings dropdowns, and administrators can review them
under Reference data → Treatments.

### Role synchronization resets declared roles

Both `seed_reference_data` and `setup_roles` set each declared role's
permissions to exactly what `apps/users/roles.py` says, including `admin`.
Anything changed on the roles screen since is overwritten. Run either only when
`roles.py` has changed or when restoring its code-defined permissions, and check
first whether anyone has edited roles by hand. Roles created on the screen are
left alone (and reported as stale); `--prune` is available only through
`setup_roles` and deletes them.

## Health checks

| Endpoint | Returns |
| --- | --- |
| `GET /api/health/` | 200 while the process runs; touches nothing else. Use for liveness. |
| `GET /api/health/ready/` | 503 if the database is unreachable or migrations are pending. Use for readiness. |

## What to back up

- The database.
- `media/` - uploaded stone photographs and avatars. Certificates print from the
  photo stored when they were issued, so losing these breaks old certificates.
- `certificates/` - the GePG signing key, if signing is on.
