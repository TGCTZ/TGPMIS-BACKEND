"""Raising in-app notifications.

The single entry point every app calls. Channels beyond the in-app inbox belong
behind these functions, so a caller never learns how a message travels.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db import transaction

from .models import Notification, subscription_codename
from .sms import send_sms


def notify(
    *,
    kind: str,
    recipients,
    title: str,
    body: str = "",
    link: str = "",
    sms_message: str | None = None,
) -> None:
    """Send one notification to each recipient once the transaction commits.

    Deferred to commit because the event may still roll back: a notification
    about an order that was never saved would send someone to a record that
    does not exist. ``robust`` because the business action has already
    committed by then - a failed notification is logged, never surfaced as an
    error on a request that actually succeeded.

    Args:
        kind: A ``NotificationKind`` value.
        recipients: Users, or a lazy queryset of them; evaluated at commit.
        title: The one-line summary the bell shows.
        body: Optional detail.
        link: A frontend route to open when the notification is clicked.
        sms_message: Optional SMS delivered to the same recipients after commit.
    """

    def deliver():
        recipient_ids = {user.pk for user in recipients}
        Notification.objects.bulk_create(
            Notification(
                recipient_id=recipient_id,
                kind=kind,
                title=title[:150],
                body=body,
                link=link,
            )
            for recipient_id in recipient_ids
        )
        if sms_message and recipient_ids:
            phone_numbers = get_user_model().objects.filter(
                pk__in=recipient_ids
            ).values_list("phone_number", flat=True)
            send_sms(phone_numbers, sms_message, context=f"{kind} notification")

    transaction.on_commit(deliver, robust=True)


def notify_subscribers(
    kind: str,
    *,
    title: str,
    body: str = "",
    link: str = "",
    sms_message: str | None = None,
    exclude=None,
) -> None:
    """Notify every active user subscribed to ``kind``.

    Subscribed means holding ``notifications.receive_<kind>``, which
    ``apps.users.roles`` grants to the one desk each handoff is for. Resolved by
    permission rather than role name, so a user in two roles hears through
    either, and an administrator can re-route a handoff from the roles screen
    without a deploy. Superuser *flags* are ignored for the same reason
    superadmin is denied these permissions: the break-glass account is not a
    desk.

    Args:
        kind: A ``NotificationKind`` value; also picks the subscription.
        title: The one-line summary the bell shows.
        body: Optional detail.
        link: A frontend route to open when the notification is clicked.
        sms_message: Optional SMS delivered to the same subscribers after commit.
        exclude: The acting user, who already knows what they just did.
    """
    perm = f"notifications.{subscription_codename(kind)}"
    recipients = ModelBackend().with_perm(perm, is_active=True, include_superusers=False)
    if exclude is not None:
        recipients = recipients.exclude(pk=exclude.pk)
    notify(
        kind=kind,
        recipients=recipients.only("pk"),
        title=title,
        body=body,
        link=link,
        sms_message=sms_message,
    )
