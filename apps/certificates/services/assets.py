"""Images embedded in a certificate PDF: the lab's marks, and the QR code.

Everything here returns a ``data:`` URI rather than a path or a URL. That is
what lets ``render_certificate_pdf`` call WeasyPrint without a ``base_url``: the
document carries its own pixels, so it renders identically from a request, a
management command, a test, or a background job, and never depends on
``collectstatic`` having run.

The cost is document size - a few hundred KB per certificate - which is the
right trade for a file that has to be reproducible years later.
"""

import base64
import logging
import mimetypes
from io import BytesIO
from pathlib import Path

import qrcode
from PIL import Image, ImageOps

from django.conf import settings
from django.utils.safestring import mark_safe

logger = logging.getLogger(__name__)

ASSET_DIR = Path(__file__).resolve().parent.parent / "static" / "certificates" / "img"

#: Filenames under :data:`ASSET_DIR`, by the name the template asks for.
#:
#: One name, one file, spelled out: a misnamed file is then reported missing,
#: rather than the loader guessing at spellings and quietly using whichever
#: similar file it finds.
ASSETS = {
    "header_banner": "header-banner.jpg",
    "coat_of_arms": "coat-of-arms.png",
    "tgc_logo": "tgc-logo.png",
    "official_stamp": "official-stamp.png",
}


def _encode(data: bytes, mime: str) -> str:
    """Wrap raw bytes as a ``data:`` URI."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


#: Encoded assets, by name. Only *hits* live here - see :func:`asset_data_uri`.
_ENCODED: dict[str, str] = {}

#: Names already reported missing, so the warning is logged once per absence
#: rather than once per certificate rendered.
_REPORTED_MISSING: set[str] = set()


def asset_data_uri(name: str) -> str | None:
    """Return one of the lab's marks as a ``data:`` URI, or None if absent.

    A file that was found is cached for the life of the process: these images
    do not change between renders, and re-reading a 500KB PNG per certificate
    is pure waste.

    A file that was *not* found is deliberately not cached: caching the miss
    would keep printing the empty box after the file is added, until a restart -
    and a new image does not trigger Django's autoreloader. Re-checking costs a
    single ``stat`` per render, and it keeps the promise in
    ``static/certificates/img``'s README: drop the file in and it appears on the
    next render.

    Returns None rather than raising when a file is missing, so a lab that has
    not supplied its stamp yet still gets a certificate - the template falls
    back to a labelled empty box.

    Args:
        name: A key of :data:`ASSETS`.
    """
    filename = ASSETS.get(name)
    if filename is None:
        return None

    return _encoded_file(name, ASSET_DIR / filename)


def _encoded_file(key: str, path: Path, mime: str | None = None) -> str | None:
    """Read and encode one file, caching the hit and never the miss.

    Shared by the lab's marks and the embedded fonts: both are files on disk
    that must reach the document as ``data:`` URIs, and both must appear the
    moment someone drops them in.

    Args:
        key: Cache key, unique across every kind of asset.
        path: The file to read.
        mime: Media type, guessed from the suffix when not given.
    """
    encoded = _ENCODED.get(key)
    if encoded is not None:
        return encoded

    if not path.is_file():
        # Logged once per absence: this runs on every render now, and a lab
        # without a stamp would otherwise fill the log with one line per
        # certificate issued.
        if key not in _REPORTED_MISSING:
            logger.warning("Certificate asset %r not found at %s", key, path)
            _REPORTED_MISSING.add(key)
        return None

    if mime is None:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = _encode(path.read_bytes(), mime)

    # Racing threads may both land here and encode the same file; both produce
    # the same bytes, so the last write wins harmlessly.
    _ENCODED[key] = encoded
    _REPORTED_MISSING.discard(key)
    return encoded


def forget_assets() -> None:
    """Drop the encoded-asset cache, so the next render re-reads from disk.

    For replacing a mark in a running process, and for tests that write an
    asset into a temporary :data:`ASSET_DIR`.
    """
    _ENCODED.clear()
    _REPORTED_MISSING.clear()


def lab_assets() -> dict[str, str | None]:
    """Every lab mark the template may want, keyed as the template names them."""
    return {name: asset_data_uri(name) for name in ASSETS}


FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "certificates" / "fonts"

#: The faces the document embeds: (file stem, CSS family, weight).
#:
#: Source Serif 4 is the institutional voice - the lab's name, the document
#: title, the column headers and every label. Inter carries the findings: the
#: values, readings and measurements. Splitting them is not decoration; a
#: refractive index reads more reliably in a face designed for small sizes and
#: even figure widths than in a text serif.
#:
#: Subset and instanced by ``scripts/build_fonts.py`` - see that file and
#: ``static/certificates/fonts/README.md``.
FONTS = (
    ("source-serif-4-regular", "TGC Serif", 400),
    ("source-serif-4-semibold", "TGC Serif", 600),
    ("inter-regular", "TGC Sans", 400),
    ("inter-semibold", "TGC Sans", 600),
    # Arial's metrics, for the text the lab's paper form sets in Arial.
    ("arimo-regular", "TGC Arial", 400),
    ("arimo-bold", "TGC Arial", 700),
)


def font_faces() -> str:
    """The ``@font-face`` rules for the document, fonts embedded inline.

    Returned as CSS text rather than a list of URIs so the template stays a
    template: it drops this in at the top of its ``<style>`` block and never
    has to know how many faces there are or what they are called.

    Embedded for the same reason the images are, but with a sharper edge. The
    render host carries DejaVu and nothing else, and the system package list
    installs no fonts at all, so a face named but not embedded does not fail -
    it *substitutes*, silently, and the lab issues a subtly different document
    without anyone noticing.

    A face whose file is missing is skipped rather than raising, and the
    template's fallback stack catches it. A certificate that renders in the
    wrong font is recoverable; one that does not render is not.
    """
    rules = []
    for stem, family, weight in FONTS:
        uri = _encoded_file(f"font:{stem}", FONT_DIR / f"{stem}.woff2", "font/woff2")
        if uri is None:
            continue
        rules.append(
            f"@font-face{{font-family:'{family}';font-style:normal;"
            f"font-weight:{weight};src:url({uri}) format('woff2');}}"
        )
    return mark_safe("\n".join(rules))  # noqa: S308 - our own files, not user input


#: Longest edge, in pixels, of a photograph embedded in a certificate.
#:
#: The photo prints into a box about 40mm across. At 300dpi that is roughly
#: 470px, so 900 leaves generous headroom for print while cutting a modern
#: phone photo - 4000px and several megabytes - down to something a document
#: can carry. The bytes are embedded per certificate, so the full-resolution
#: original turned a 250KB document into a 1.9MB one for detail no printer
#: could resolve.
PHOTO_MAX_EDGE = 900


def photo_data_uri(image_field) -> str | None:
    """Return an uploaded image as a ``data:`` URI, or None if there is none.

    Downscaled to :data:`PHOTO_MAX_EDGE` and re-encoded as JPEG. The original
    upload is left untouched on disk - this only governs what goes into the
    document.

    Reads through the storage backend rather than ``.path``, so this keeps
    working when media moves off the local disk - ``.path`` raises
    ``NotImplementedError`` on any remote backend.

    Args:
        image_field: An ``ImageField`` value, which may be empty.
    """
    if not image_field:
        return None

    try:
        with image_field.open("rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        # A row pointing at a file that is no longer there must not take the
        # whole certificate down; the template renders its placeholder instead.
        logger.warning("Certificate photo unreadable: %s", image_field.name)
        return None

    try:
        with Image.open(BytesIO(data)) as original:
            # Bake in the EXIF orientation before anything else. A phone writes
            # the sensor's pixels and a "rotate me" tag; re-encoding drops the
            # tag, so without this a portrait photograph prints on its side.
            image = ImageOps.exif_transpose(original)

            # Flatten next: a PNG or a phone HEIC can carry transparency, and
            # JPEG has no alpha channel to put it in.
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.thumbnail((PHOTO_MAX_EDGE, PHOTO_MAX_EDGE), Image.LANCZOS)

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=85, optimize=True)
            return _encode(buffer.getvalue(), "image/jpeg")
    except (OSError, ValueError):
        # Not something Pillow recognises. Embed it as uploaded rather than
        # dropping the photograph from the document entirely.
        logger.warning("Certificate photo could not be resized: %s", image_field.name)
        mime = mimetypes.guess_type(image_field.name)[0] or "image/jpeg"
        return _encode(data, mime)


def verification_url(certificate_number: str) -> str:
    """The public URL a certificate's QR code points at.

    Args:
        certificate_number: The certificate's own number, e.g. ``CERT-2627-1-1``.
    """
    base = settings.CERTIFICATE_VERIFY_BASE_URL.rstrip("/")
    return f"{base}/verify/{certificate_number}/"


def qr_data_uri(payload: str) -> str:
    """Render a QR code for ``payload`` as a PNG ``data:`` URI.

    Error correction is set to M rather than qrcode's default L. A certificate
    is handled, folded and photocopied, and M tolerates roughly 15% damage
    against L's 7% - worth the slightly denser code on a mark that has to scan
    off paper years later.

    Args:
        payload: The text to encode, normally a verification URL.
    """
    code = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=1,
    )
    code.add_data(payload)
    code.make(fit=True)

    buffer = BytesIO()
    code.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
    return _encode(buffer.getvalue(), "image/png")
