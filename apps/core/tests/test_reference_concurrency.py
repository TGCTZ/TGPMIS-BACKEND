"""PostgreSQL integration coverage for the annual counter's row lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from django.db import close_old_connections, connection

from apps.orders.models import Customer, Order
from apps.orders.services import create_order
from apps.orders.tests.factories import CustomerFactory


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="SELECT FOR UPDATE concurrency is exercised on PostgreSQL.",
)
def test_concurrent_order_creation_allocates_distinct_numbers():
    """Two first-time allocators for a year cannot claim the same reference."""
    customer = CustomerFactory()
    customer_id = customer.pk
    barrier = Barrier(2)

    def create_order_concurrently():
        close_old_connections()
        try:
            customer_for_worker = Customer.objects.get(pk=customer_id)
            barrier.wait(timeout=10)
            return create_order(customer=customer_for_worker, stone_count=1)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(lambda _: create_order_concurrently(), range(2)))

    references = [order.reference_number for order in created]
    assert len(set(references)) == 2
    assert Order.objects.filter(reference_number__in=references).count() == 2
