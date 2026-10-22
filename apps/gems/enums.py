"""Every enum the TGC domain uses.

They live together at L2, below the apps that own the corresponding tables,
because several of them are read across app boundaries: the findings
worklist in ``identification`` filters on ``BillStatus.PAID``, and the
certification worklist does the same. Keeping the enums here lets those apps sit
at the same layer without importing one another - the join itself
(``order__bill__status``) is a plain ORM traversal that needs no import.
"""

from django.db import models


class StoneStatus(models.TextChoices):
    """Per-stone lifecycle stages.

    Only ``received``, ``billed``, ``paid`` and ``certified`` are written by any
    code path today; the rest describe stages of the documented workflow that
    have no implementation yet. They are kept so a client can render a stone
    whose status was set by hand, and so the intended lifecycle stays visible.
    """

    RECEIVED = ("received", "Received")
    UNDER_IDENTIFICATION = ("under_identification", "Under identification")
    BILLED = ("billed", "Billed")
    PAID = ("paid", "Paid")
    CERTIFIED = ("certified", "Certified")
    READY_FOR_COLLECTION = ("ready_for_collection", "Ready for collection")
    COLLECTED = ("collected", "Collected")
    ON_HOLD = ("on_hold", "On hold")
    CANCELLED = ("cancelled", "Cancelled")


class ColorGroup(models.TextChoices):
    """Broad color families used to group the color lookup (GIA-style)."""

    WHITE_GREY_BLACK = ("white_grey_black", "White/Grey/Black")
    PURPLE_VIOLET = ("purple_violet", "Purple/Violet")
    RED_PINK = ("red_pink", "Red/Pink")
    ORANGE_YELLOW = ("orange_yellow", "Orange/Yellow")
    GREEN = ("green", "Green")
    BLUE = ("blue", "Blue")
    BROWN = ("brown", "Brown")


class WeightUnit(models.TextChoices):
    """Unit for stone weight."""

    CARAT = ("carat", "Carat")
    GRAM = ("gram", "Gram")
    KILOGRAM = ("kilogram", "Kilogram")

    @property
    def symbol(self) -> str:
        """Short display symbol, e.g. 'ct' or 'g'."""
        return {self.CARAT: "ct", self.GRAM: "g", self.KILOGRAM: "kg"}[self]


class Transparency(models.TextChoices):
    """How light passes through the stone."""

    TRANSPARENT = ("transparent", "Transparent")
    TRANSLUCENT = ("translucent", "Translucent")
    OPAQUE = ("opaque", "Opaque")


class NatureType(models.TextChoices):
    """Whether the stone is natural or man-made/altered."""

    NATURAL = ("natural", "Natural")
    ARTIFICIAL = ("artificial", "Artificial")
    SYNTHETIC = ("synthetic", "Synthetic")


class OpticCharacter(models.TextChoices):
    """Optical behavior under polarized light.

    The stored value is the lowercase code; the label is the uppercase code.
    """

    SR = ("sr", "SR")
    ADR = ("adr", "ADR")
    DR = ("dr", "DR")
    AGG = ("agg", "AGG")


class BillStatus(models.TextChoices):
    """Payment state of a bill.

    ``cancelled`` and ``expired`` have no code path yet: ``Bill.expiry_at`` is
    recorded but nothing acts on it, and bill cancellation against the gateway is
    not implemented.
    """

    PENDING = ("pending", "Pending")
    PARTIALLY_PAID = ("partially_paid", "Partially paid")
    PAID = ("paid", "Paid")
    CANCELLED = ("cancelled", "Cancelled")
    EXPIRED = ("expired", "Expired")


class OrderHold(models.TextChoices):
    """A decision made about a whole order, rather than about its stones.

    The one piece of an order's state that cannot be derived from its stones and
    its bill: "the customer asked us to pause" and "the customer withdrew" are
    facts about the visit, not about any stone. Everything else an order's stage
    can say is a function of what its stones have done, and stays derived.
    """

    ACTIVE = ("active", "Active")
    ON_HOLD = ("on_hold", "On hold")
    CANCELLED = ("cancelled", "Cancelled")


class OrderStage(models.TextChoices):
    """Where a whole order has got to, derived rather than stored.

    An order carries no status column, deliberately: progress is per-stone, and
    two stones from one visit can sit at different stages. But a list of orders
    still has to answer "where is this one?", and the honest summary is the
    stage its *least advanced* stone has reached - an order is not ready to
    collect while one of its stones is still on the bench.

    Derived by :func:`apps.orders.selectors.order_stage`, so nothing can drift
    out of step with the stone statuses and bill it is computed from.
    """

    IDENTIFYING = ("identifying", "Awaiting identification")
    READY_TO_BILL = ("ready_to_bill", "Ready to bill")
    BILLING_ATTENTION = ("billing_attention", "Billing needs attention")
    AWAITING_PAYMENT = ("awaiting_payment", "Awaiting payment")
    PART_PAID = ("part_paid", "Partly paid")
    IN_FINDINGS = ("in_findings", "Findings in progress")
    CERTIFIED = ("certified", "Certified")
    READY_FOR_COLLECTION = ("ready_for_collection", "Ready for collection")
    COLLECTED = ("collected", "Collected")
    ON_HOLD = ("on_hold", "On hold")
    CANCELLED = ("cancelled", "Cancelled")
    EMPTY = ("empty", "No stones yet")


class CertificateStatus(models.TextChoices):
    """Certificate lifecycle states, including a legacy revoked value.

    New certificates are issued as ``issued``. ``revoked`` remains readable for
    historical rows; ``reissued`` is unreachable because there is no re-issue
    service.
    """

    ISSUED = ("issued", "Issued")
    REVOKED = ("revoked", "Revoked")
    REISSUED = ("reissued", "Reissued")


class Region(models.TextChoices):
    """Tanzania's administrative regions (mikoa) - where a customer is from.

    All 31: the 26 on the mainland, then the 5 in Zanzibar, each alphabetical.
    Checked against the official list in September 2026; the newest is Songwe,
    split from Mbeya in 2016. Labels are the official Swahili names - Pwani, not
    Coast; Kaskazini Pemba, not Pemba North - and the English names that people
    also type are matched by :func:`apps.gems.regions.normalize_region`.

    Mirrored for the dropdowns in the frontend's ``src/lib/regions.ts``.
    """

    ARUSHA = ("arusha", "Arusha")
    DAR_ES_SALAAM = ("dar_es_salaam", "Dar es Salaam")
    DODOMA = ("dodoma", "Dodoma")
    GEITA = ("geita", "Geita")
    IRINGA = ("iringa", "Iringa")
    KAGERA = ("kagera", "Kagera")
    KATAVI = ("katavi", "Katavi")
    KIGOMA = ("kigoma", "Kigoma")
    KILIMANJARO = ("kilimanjaro", "Kilimanjaro")
    LINDI = ("lindi", "Lindi")
    MANYARA = ("manyara", "Manyara")
    MARA = ("mara", "Mara")
    MBEYA = ("mbeya", "Mbeya")
    MOROGORO = ("morogoro", "Morogoro")
    MTWARA = ("mtwara", "Mtwara")
    MWANZA = ("mwanza", "Mwanza")
    NJOMBE = ("njombe", "Njombe")
    PWANI = ("pwani", "Pwani")
    RUKWA = ("rukwa", "Rukwa")
    RUVUMA = ("ruvuma", "Ruvuma")
    SHINYANGA = ("shinyanga", "Shinyanga")
    SIMIYU = ("simiyu", "Simiyu")
    SINGIDA = ("singida", "Singida")
    SONGWE = ("songwe", "Songwe")
    TABORA = ("tabora", "Tabora")
    TANGA = ("tanga", "Tanga")
    # Zanzibar
    KASKAZINI_PEMBA = ("kaskazini_pemba", "Kaskazini Pemba")
    KASKAZINI_UNGUJA = ("kaskazini_unguja", "Kaskazini Unguja")
    KUSINI_PEMBA = ("kusini_pemba", "Kusini Pemba")
    KUSINI_UNGUJA = ("kusini_unguja", "Kusini Unguja")
    MJINI_MAGHARIBI = ("mjini_magharibi", "Mjini Magharibi")
