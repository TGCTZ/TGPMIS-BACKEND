"""Human interaction, rather than ordinary API traffic, extends a login."""

from datetime import timedelta

import pytest

from django.utils import timezone

from apps.users.models import AuthSession

pytestmark = pytest.mark.django_db


def _login(api_client, user):
    response = api_client.post(
        "/api/v1/auth/login/", {"email": user.email, "password": "TestPass!2026"}
    )
    assert response.status_code == 200
    return response.data


def test_api_reads_do_not_extend_idle_session(api_client, user):
    """Read-only API requests must not reset a login's idle deadline."""
    tokens = _login(api_client, user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    session = AuthSession.objects.get(user=user)
    before = session.last_interaction_at

    assert api_client.get("/api/v1/auth/me/").status_code == 200
    session.refresh_from_db()
    assert session.last_interaction_at == before


def test_interaction_extends_only_its_login(api_client, user):
    """An activity request updates only the session owning its access token."""
    first = _login(api_client, user)
    _login(api_client, user)
    sessions = list(AuthSession.objects.filter(user=user).order_by("last_interaction_at"))
    old = timezone.now() - timedelta(minutes=59)
    AuthSession.objects.filter(user=user).update(last_interaction_at=old)

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {first['access']}")
    assert api_client.post("/api/v1/auth/activity/").status_code == 204

    sessions[0].refresh_from_db()
    sessions[1].refresh_from_db()
    assert sum(session.last_interaction_at > old for session in sessions) == 1


def test_expired_login_rejects_access_and_refresh(api_client, user):
    """Expired sessions must reject both access and refresh tokens."""
    tokens = _login(api_client, user)
    AuthSession.objects.filter(user=user).update(
        last_interaction_at=timezone.now() - timedelta(minutes=61)
    )

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    assert api_client.get("/api/v1/auth/me/").status_code == 401
    api_client.credentials()
    refresh = api_client.post("/api/v1/auth/refresh/", {"refresh": tokens["refresh"]})
    assert refresh.status_code == 401


def test_logout_invalidates_already_issued_access_token(api_client, user):
    """Logging out must invalidate access tokens issued for that login."""
    tokens = _login(api_client, user)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    logout = api_client.post("/api/v1/auth/logout/", {"refresh": tokens["refresh"]})
    assert logout.status_code == 205
    assert api_client.get("/api/v1/auth/me/").status_code == 401
