"""Transactional annual order-number counters."""

from django.db import models


class OrderSequence(models.Model):
    """The last order sequence allocated in one Tanzanian financial year.

    This is an operational counter rather than a business record, so it is not
    soft-deleted or audit-logged. The sequence is text because Python converts
    it to an arbitrary-precision integer during allocation; this avoids adding a
    fixed numeric ceiling. The row lock serializes allocations in one year.
    """

    financial_year = models.CharField(max_length=4, primary_key=True)
    last_sequence = models.TextField(default="0")

    class Meta:
        ordering = ["financial_year"]

    def __str__(self) -> str:
        return f"{self.financial_year}: {self.last_sequence}"
