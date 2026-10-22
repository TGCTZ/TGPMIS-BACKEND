# Accounts and first login

There is no self-registration. Every account is created by a superadmin, admin
or manager, from an email and a role. No credentials email is sent. The creator
shares the temporary password manually; on first sign-in, the new user must set
their own password and complete their profile before the system opens up to them.

Code: `apps/users/services/accounts.py` (the flow),
`apps/users/authentication.py` (the first-login and idle-session rules).

## Inactivity timeout

Each login creates an `AuthSession` identified by the JWT's `sid` claim. The API
rejects access and refresh tokens once that session has gone 60 minutes without
a reported human interaction. Ordinary API requests do not extend the deadline.

The frontend listens for pointer, click, keyboard, input and wheel events and sends a throttled
`POST /api/v1/auth/activity/` when the user interacts. It keeps a local deadline
for prompt sign-out, including after a sleeping tab wakes, and shares the last
interaction time across tabs. The backend checks its own deadline on every
authenticated request and before refresh-token rotation. Logout revokes the
session, so already-issued access tokens also stop working. A deployment of
this change requires users holding older JWTs without `sid` to sign in again.

## Creating an account

`POST /api/v1/users/` with `{"email": ..., "role": ...}`. Requires
`users.add_user`, and the role must rank below the creator's own
([the hierarchy](permissions.md#the-hierarchy)) - roles above the creator are not
even resolvable, so naming one is a 400 "does not exist".

`create_user_account()` sets everything else:

| Field | Value |
| --- | --- |
| `username` | The email's local part, lower-cased; `asha2`, `asha3`… if taken (deleted accounts count). |
| `country` | `Tanzania` |
| `user_status` | `Active` (created if the lookup row is missing) |
| password | 10 random characters from an alphabet without 0/O or 1/l/I |
| `must_change_password`, `must_complete_profile` | `True` |
| names | blank - the user fills them in |

The response is the new user **plus `temporary_password`**. It is the only time
the API returns it, so the Users screen shows it to the creator once to share
manually with the intended user.

### Why a random password per account

A shared default password (the same for everyone) lets anyone who knows a new
colleague's email sign in before they do and take the account. A random one
closes that window at no cost to the flow.

## First login

While either flag is set, `OnboardingJWTAuthentication` - the project's only
authentication class - refuses every request outside `FIRST_LOGIN_ALLOWED` with a
403 whose body carries `"code": "first_login_required"`:

| URL name | Methods | Why |
| --- | --- | --- |
| `auth-login`, `auth-refresh`, `auth-logout`, `auth-activity` | any | Signing in and out, and extending the idle deadline |
| `auth-me` | GET | Reading who you are (not editing - the profile step does that) |
| `auth-first-login-password`, `auth-first-login-profile` | any | The two steps |
| `config` | any | Deployment flags the client renders with |
| `gender-list` | GET | The profile step's gender options |

The rule is enforced here rather than left to the frontend, because a redirect in
the UI can be skipped with a token and curl.

The steps, in order:

1. `POST /auth/first-login/password/` `{password, password_confirm}` - Django's
   password validators apply, and the temporary password is refused. Clears
   `must_change_password`, blacklists outstanding refresh tokens and **returns
   a fresh token pair**. Existing `AuthSession` rows are not revoked, so an
   already-issued access token can remain usable until expiry or idle timeout.
2. `POST /auth/first-login/profile/` `{first_name, last_name, phone_number,
   gender, middle_name?}` - refused while step 1 is due, so a leaked temporary
   password cannot finish the setup. Clears `must_complete_profile`.

The frontend follows the flags: `/first-login` shows whichever step is due, the
authenticated-route guard sends flagged users there, and a `first_login_required`
403 from any query does the same.

### Allowing another endpoint during first login

Add its URL name to `FIRST_LOGIN_ALLOWED`, with the narrowest set of methods that
works, and add a case to `apps/users/tests/test_first_login.py`. Anything a new
user can reach before finishing is reachable with only the emailed password.

## Resetting a password

`POST /api/v1/users/{id}/reset-password/` issues a new temporary password,
returns it once for manual sharing, sets `must_change_password`, and blacklists outstanding
refresh tokens. It requires `users.change_user` and follows the hierarchy (a
peer is refused, a superior is not even visible). It refuses your own account -
that is changed from Settings, where the current password is checked. The profile
is left as it was. Active `AuthSession` rows are not revoked, so existing access
tokens remain usable until expiry or the session idle timeout.

It is also the system's answer to a forgotten password: there is no self-service
reset.

## Accounts from before this flow

Both flags default to `False`, so accounts created before it - and the demo
accounts from `seed` - sign in as usual.

## Tests

`apps/users/tests/test_accounts.py` (creation, manual credential handoff, reset) and
`apps/users/tests/test_first_login.py` (the rule and both steps).
