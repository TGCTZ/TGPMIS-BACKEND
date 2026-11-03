"""Notification fan-out, the pipeline handoffs that raise them, and the inbox API."""

from decimal import Decimal

import pytest

from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.db import transaction

from apps.billing.dev import simulate_payment
from apps.billing.services import generate_bill_for_order
from apps.certificates.services import issue_certificate
from apps.gems.tests.factories import StoneTypeFactory
from apps.identification.tests.factories import (
    create_finalizable_report,
    finalize_complete_report,
)
from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import notify, notify_subscribers
from apps.orders.services import add_stone, create_order, update_stone
from apps.orders.tests.factories import CustomerFactory
from apps.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def accountant_user(roles):
    """A user holding the accountant role - the desk that bills."""
    account = UserFactory()
    account.groups.add(Group.objects.get(name="accountant"))
    return account


@pytest.fixture
def superadmin_user(roles):
    """A user holding the break-glass superadmin role."""
    account = UserFactory()
    account.groups.add(Group.objects.get(name="superadmin"))
    return account


def _kinds_for(user) -> list[str]:
    return list(
        Notification.objects.filter(recipient=user)
        .order_by("created_at", "id")
        .values_list("kind", flat=True)
    )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def test_notify_waits_for_the_commit(django_capture_on_commit_callbacks, user):
    """Nothing is written until the surrounding transaction commits."""
    with django_capture_on_commit_callbacks() as callbacks:
        notify(kind=NotificationKind.ORDER_RECEIVED, recipients=[user], title="Hi")
        assert not Notification.objects.exists()

    for callback in callbacks:
        callback()
    assert Notification.objects.filter(recipient=user).count() == 1


def test_a_rolled_back_event_notifies_nobody(django_capture_on_commit_callbacks, user):
    """A notification about work that never happened would point at nothing."""
    with (
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        notify(kind=NotificationKind.ORDER_RECEIVED, recipients=[user], title="Hi")
        raise RuntimeError

    assert not Notification.objects.exists()


def test_subscribers_are_notified_and_the_actor_is_not(
    django_capture_on_commit_callbacks, gemmologist_user, viewer_user
):
    """The desk's other members hear; the actor and other desks do not."""
    second_gemmologist = UserFactory()
    second_gemmologist.groups.add(Group.objects.get(name="gemmologist"))

    with django_capture_on_commit_callbacks(execute=True):
        notify_subscribers(
            NotificationKind.ORDER_RECEIVED,
            title="New order",
            exclude=gemmologist_user,
        )

    assert _kinds_for(second_gemmologist) == [NotificationKind.ORDER_RECEIVED]
    assert _kinds_for(gemmologist_user) == []
    assert _kinds_for(viewer_user) == []


def test_inactive_users_are_not_notified(django_capture_on_commit_callbacks, roles):
    """A deactivated account keeps its groups but should stop collecting work."""
    former = UserFactory(is_active=False)
    former.groups.add(Group.objects.get(name="gemmologist"))

    with django_capture_on_commit_callbacks(execute=True):
        notify_subscribers(NotificationKind.ORDER_RECEIVED, title="New order")

    assert not Notification.objects.exists()


def test_holding_the_action_is_not_a_subscription(
    django_capture_on_commit_callbacks, admin_user, superadmin_user
):
    """Manager and superadmin may add stones, but no new order waits on them."""
    assert admin_user.has_perm("orders.add_stone")
    assert superadmin_user.has_perm("orders.add_stone")

    with django_capture_on_commit_callbacks(execute=True):
        notify_subscribers(NotificationKind.ORDER_RECEIVED, title="New order")

    assert _kinds_for(admin_user) == []
    assert _kinds_for(superadmin_user) == []


def test_a_manager_who_also_works_the_bench_hears_as_the_bench(
    django_capture_on_commit_callbacks, admin_user
):
    """Subscriptions follow any role held, so a second role is not drowned out."""
    admin_user.groups.add(Group.objects.get(name="gemmologist"))

    with django_capture_on_commit_callbacks(execute=True):
        notify_subscribers(NotificationKind.ORDER_RECEIVED, title="New order")

    assert _kinds_for(admin_user) == [NotificationKind.ORDER_RECEIVED]


def test_superadmin_gets_every_permission_but_subscriptions(roles):
    """setup_roles grants superadmin everything except the notification opt-ins."""
    granted = set(
        Group.objects.get(name="superadmin").permissions.values_list(
            "content_type__app_label", "codename"
        )
    )
    subscriptions = set(
        Permission.objects.filter(content_type__app_label="notifications").values_list(
            "content_type__app_label", "codename"
        )
    )

    assert len(subscriptions) == len(NotificationKind.values)
    assert not granted & subscriptions
    assert ("orders", "add_stone") in granted


def test_the_default_model_permissions_do_not_exist(db):
    """Nothing checks them, so they must not appear in the roles matrix."""
    call_command("setup_roles", verbosity=0)
    codenames = set(
        Permission.objects.filter(content_type__app_label="notifications").values_list(
            "codename", flat=True
        )
    )

    assert codenames == {f"receive_{kind}" for kind in NotificationKind.values}


# ---------------------------------------------------------------------------
# Pipeline handoffs
# ---------------------------------------------------------------------------


def test_each_station_hears_when_the_order_reaches_it(
    django_capture_on_commit_callbacks,
    settings,
    gemmologist_user,
    accountant_user,
    viewer_user,
    admin_user,
    superadmin_user,
):
    """Walk one order through the lab; each desk is told once, when it is up."""
    settings.GEPG_SIMULATE = True

    with django_capture_on_commit_callbacks(execute=True):
        order = create_order(customer=CustomerFactory(), stone_count=2)
    assert _kinds_for(gemmologist_user) == [NotificationKind.ORDER_RECEIVED]

    with django_capture_on_commit_callbacks(execute=True):
        first = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("100")))
    assert _kinds_for(accountant_user) == [], "one stone is still untyped"

    with django_capture_on_commit_callbacks(execute=True):
        second = add_stone(order, stone_type=StoneTypeFactory(price=Decimal("100")))
    assert _kinds_for(accountant_user) == [NotificationKind.READY_TO_BILL]

    with django_capture_on_commit_callbacks(execute=True):
        simulate_payment(generate_bill_for_order(order))
    assert NotificationKind.BILL_PAID in _kinds_for(gemmologist_user)

    finalizer = UserFactory()
    for stone in (first, second):
        stone.refresh_from_db()
        update_stone(stone, weight=Decimal("1.000"))
        with django_capture_on_commit_callbacks(execute=True):
            report = create_finalizable_report(stone, finalizer)
            finalize_complete_report(report, finalizer)
    assert NotificationKind.READY_TO_CERTIFY not in _kinds_for(gemmologist_user)

    for stone in (first, second):
        stone.refresh_from_db()
        with django_capture_on_commit_callbacks(execute=True):
            issue_certificate(stone, user=gemmologist_user)
    assert _kinds_for(viewer_user) == [NotificationKind.READY_FOR_COLLECTION]

    # Each desk heard only its own handoffs, and oversight heard none.
    assert _kinds_for(gemmologist_user) == [
        NotificationKind.ORDER_RECEIVED,
        NotificationKind.BILL_PAID,
    ]
    assert _kinds_for(accountant_user) == [NotificationKind.READY_TO_BILL]
    assert _kinds_for(admin_user) == []
    assert _kinds_for(superadmin_user) == []


# ---------------------------------------------------------------------------
# Inbox API
# ---------------------------------------------------------------------------


def _note(recipient, **kwargs):
    return Notification.objects.create(
        recipient=recipient, kind=NotificationKind.ORDER_RECEIVED, title="t", **kwargs
    )


def test_the_inbox_shows_only_my_notifications(user, auth_client):
    """Another user's notifications never appear in my list."""
    mine = _note(user)
    _note(UserFactory())

    response = auth_client(user).get("/api/v1/notifications/")

    assert response.status_code == 200
    assert [row["id"] for row in response.data["results"]] == [mine.id]


def test_unread_filter(user, auth_client):
    """``unread=1`` drops what has already been read."""
    unread = _note(user)
    _note(user, read_at="2026-01-01T00:00:00Z")

    listed = auth_client(user).get("/api/v1/notifications/", {"unread": "1"})

    assert [row["id"] for row in listed.data["results"]] == [unread.id]


def test_unread_summary_counts_per_route(user, auth_client):
    """Query strings fold into their route, so the sidebar badges one entry."""
    _note(user, link="/worklists/billing")
    _note(user, link="/orders?search=ORD-1")
    _note(user)
    newest = _note(user, link="/orders?search=ORD-2")
    _note(user, link="/worklists/billing", read_at="2026-01-01T00:00:00Z")
    _note(UserFactory(), link="/worklists/billing")

    summary = auth_client(user).get("/api/v1/notifications/unread-summary/").data

    assert summary["count"] == 4
    assert summary["by_link"] == {"/worklists/billing": 1, "/orders": 2}
    assert summary["latest"]["id"] == newest.id


def test_unread_summary_when_caught_up(user, auth_client):
    """An empty inbox still answers with the full shape."""
    summary = auth_client(user).get("/api/v1/notifications/unread-summary/").data

    assert summary == {"count": 0, "by_link": {}, "latest": None}


def test_marking_read_is_idempotent(user, auth_client):
    """Marking twice keeps the first read time."""
    note = _note(user)
    client = auth_client(user)

    first = client.post(f"/api/v1/notifications/{note.id}/read/")
    second = client.post(f"/api/v1/notifications/{note.id}/read/")

    assert first.status_code == 200
    assert first.data["is_read"] is True
    assert second.data["read_at"] == first.data["read_at"]


def test_someone_elses_notification_is_not_found(user, auth_client):
    """A 404, not a 403: the API does not confirm the row exists."""
    theirs = _note(UserFactory())

    response = auth_client(user).post(f"/api/v1/notifications/{theirs.id}/read/")

    assert response.status_code == 404
    theirs.refresh_from_db()
    assert theirs.read_at is None


def test_read_all_touches_only_my_unread(user, auth_client):
    """Read-all never reaches another user's inbox."""
    _note(user)
    _note(user)
    theirs = _note(UserFactory())

    response = auth_client(user).post("/api/v1/notifications/read-all/")

    assert response.data == {"updated": 2}
    theirs.refresh_from_db()
    assert theirs.read_at is None


def test_read_all_for_a_route_leaves_other_routes_unread(user, auth_client):
    """Opening a page clears that page's badge - whichever order it was for."""
    plain = _note(user, link="/orders")
    searched = _note(user, link="/orders?search=ORD-1")
    lookalike = _note(user, link="/orders-archive")
    elsewhere = _note(user, link="/worklists/billing")

    response = auth_client(user).post(
        "/api/v1/notifications/read-all/", {"link": "/orders"}
    )

    assert response.data == {"updated": 2}
    for note, read in (
        (plain, True),
        (searched, True),
        (lookalike, False),
        (elsewhere, False),
    ):
        note.refresh_from_db()
        assert note.is_read is read


def test_the_inbox_requires_authentication(api_client):
    """There is no anonymous inbox."""
    assert api_client.get("/api/v1/notifications/").status_code == 401
