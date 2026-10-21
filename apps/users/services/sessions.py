"""Issue JWTs tied to a login and enforce its human-interaction deadline."""

from datetime import timedelta
from uuid import UUID

from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

from django.utils import timezone

from apps.users.models import AuthSession

IDLE_TIMEOUT = timedelta(minutes=60)


def issue_session_refresh(user) -> RefreshToken:
    """Create a login session and return its refresh token with a session claim."""
    refresh = RefreshToken.for_user(user)
    session = AuthSession.objects.create(user=user)
    refresh["sid"] = str(session.pk)
    return refresh


def active_session(user_id, session_id) -> AuthSession:
    """Reject missing, revoked, or idle sessions before accepting either JWT type."""
    try:
        session_uuid = UUID(str(session_id))
    except (TypeError, ValueError, AttributeError):
        raise AuthenticationFailed("This login session is no longer valid.") from None

    session = AuthSession.objects.filter(pk=session_uuid, user_id=user_id).first()
    if session is None or session.revoked_at is not None:
        raise AuthenticationFailed("This login session is no longer valid.")

    now = timezone.now()
    if session.last_interaction_at <= now - IDLE_TIMEOUT:
        AuthSession.objects.filter(pk=session.pk, revoked_at__isnull=True).update(
            revoked_at=now
        )
        raise AuthenticationFailed("Session expired after 60 minutes of inactivity.")
    return session


def record_interaction(user_id, session_id) -> None:
    """Move the deadline only when an authenticated browser reports interaction."""
    session = active_session(user_id, session_id)
    now = timezone.now()
    updated = AuthSession.objects.filter(
        pk=session.pk,
        revoked_at__isnull=True,
        last_interaction_at__gt=now - IDLE_TIMEOUT,
    ).update(last_interaction_at=now)
    if not updated:
        raise AuthenticationFailed("Session expired after 60 minutes of inactivity.")


def revoke_session(session_id) -> None:
    """End one login immediately, including access tokens already issued for it."""
    try:
        session_uuid = UUID(str(session_id))
    except (TypeError, ValueError, AttributeError):
        return
    AuthSession.objects.filter(pk=session_uuid, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
