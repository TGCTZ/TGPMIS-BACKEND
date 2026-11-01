"""Authentication flows."""

import pytest

from apps.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_there_is_no_self_registration(api_client):
    """Accounts are created by staff only; the public sign-up route is gone."""
    response = api_client.post(
        "/api/v1/auth/register/",
        {"email": "ada@example.com", "password": "Str0ng!Passphrase"},
    )

    assert response.status_code == 404


def test_login_returns_a_token_pair_and_the_user(api_client, user):
    """Valid credentials yield access, refresh and the serialised user."""
    response = api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "TestPass!2026"}
    )

    assert response.status_code == 200, response.data
    assert "access" in response.data
    assert "refresh" in response.data
    assert response.data["user"]["email"] == user.email


def test_login_stamps_last_login_at(api_client, user):
    """A successful login records when it happened."""
    assert user.last_login_at is None

    api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "TestPass!2026"}
    )

    user.refresh_from_db()
    assert user.last_login_at is not None


def test_login_rejects_bad_credentials(api_client, user):
    """A wrong password does not authenticate."""
    response = api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "wrong"}
    )

    assert response.status_code == 401


def test_soft_deleted_user_cannot_log_in(api_client, user):
    """A deleted account is excluded by the default manager, so login fails."""
    user.delete()

    response = api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "TestPass!2026"}
    )

    assert response.status_code == 401


def test_me_requires_authentication(api_client):
    """The profile endpoint is not public."""
    assert api_client.get("/api/v1/auth/me/").status_code == 401


def test_me_returns_the_requesting_user(user, auth_client):
    """/auth/me/ resolves to the token's own user, with permissions attached."""
    response = auth_client(user).get("/api/v1/auth/me/")

    assert response.status_code == 200
    assert response.data["email"] == user.email
    assert response.data["is_superuser"] is False
    assert "permissions" in response.data


def test_django_superuser_is_identified_without_an_assigned_role(api_client, auth_client):
    """The client can display the top-level account without inventing a group."""
    superuser = UserFactory(is_superuser=True)
    assert not superuser.groups.exists()

    response = auth_client(superuser).get("/api/v1/auth/me/")

    assert response.status_code == 200
    assert response.data["is_superuser"] is True
    assert response.data["roles"] == []

    login = api_client.post(
        "/api/v1/auth/login/",
        {"email": superuser.email, "password": "TestPass!2026"},
    )
    assert login.status_code == 200
    assert login.data["user"]["is_superuser"] is True


def test_logout_blacklists_the_refresh_token(api_client, user):
    """After logout the refresh token can no longer be exchanged."""
    tokens = api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "TestPass!2026"}
    ).data
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    logout = api_client.post("/api/v1/auth/logout/", {"refresh": tokens["refresh"]})
    assert logout.status_code == 205

    api_client.credentials()
    refresh = api_client.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]})
    assert refresh.status_code == 401


def test_change_password_revokes_other_sessions(api_client, user, auth_client):
    """Changing a password invalidates refresh tokens issued before it."""
    tokens = api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "TestPass!2026"}
    ).data

    client = auth_client(user)
    response = client.post(
        "/api/v1/auth/password/",
        {
            "current_password": "TestPass!2026",
            "password": "An0ther!Passphrase",
            "password_confirm": "An0ther!Passphrase",
        },
    )
    assert response.status_code == 204

    user.refresh_from_db()
    assert user.check_password("An0ther!Passphrase")

    api_client.credentials()
    stale = api_client.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]})
    assert stale.status_code == 401


def test_change_password_rejects_a_wrong_current_password(user, auth_client):
    """The old password must be proven before it can be replaced."""
    response = auth_client(user).post(
        "/api/v1/auth/password/",
        {
            "current_password": "not-it",
            "password": "An0ther!Passphrase",
            "password_confirm": "An0ther!Passphrase",
        },
    )

    assert response.status_code == 400


def test_api_writes_record_the_acting_user(admin_user, auth_client):
    """created_by/updated_by are stamped on a write made over the API.

    Regression test: DRF authenticates inside the view, so without
    APIAuthenticationMiddleware resolving the token first, CurrentUserMiddleware
    sees AnonymousUser and every API-created row is left unattributed.
    """
    from apps.gems.models import Species

    response = auth_client(admin_user).post("/api/v1/species/", {"name": "Attributed"})

    assert response.status_code == 201, response.data
    species = Species.objects.get(name="Attributed")
    assert species.created_by == admin_user
    assert species.updated_by == admin_user


def test_api_writes_are_attributed_in_the_activity_log(admin_user, auth_client):
    """The audit trail names the actor rather than falling back to 'System'."""
    from auditlog.models import LogEntry

    response = auth_client(admin_user).post("/api/v1/species/", {"name": "Logged"})
    assert response.status_code == 201, response.data

    entry = LogEntry.objects.filter(object_repr="Logged").first()
    assert entry is not None
    assert entry.actor == admin_user
