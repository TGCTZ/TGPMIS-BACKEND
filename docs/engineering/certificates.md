# Certificates

A certificate is the lab's output: one printed page, per stone, stating what the
bench found. Everything in this app follows from a single property of that page
— **it leaves the building**. Someone will hold it in five years and ask whether
it is genuine and whether it still stands, long after the database has moved on.

That is what makes this app different from the rest of the system. Elsewhere,
reading current data is correct. Here it would be a bug.

## The snapshot rule

A certificate stores its own copy of every finding it prints. Roughly two dozen
`*_snapshot` columns on `apps/certificates/models/certificate.py` hold the stone
type, weight and unit, colour, origin, species, variety, shape and cut,
transparency, optic character, treatment, nature type, refractive index, specific gravity, comments, instruments, report number, both
gemmologists' names, and the photograph.

All of that is reachable through `certificate.report`. It is deliberately not
read that way.

> Renaming a colour in the lookup table, or correcting a report after the fact,
> must never change what a document already in a customer's hands says.

The `report` foreign key is kept for **provenance** — so you can ask which
report produced this certificate — not for rendering. `certificate_context()` in
`services/pdf.py` reaches the live report for exactly three things, none of
which are statements about the findings: the stone's label, the order it arrived
on, and the customer's name.

Four details of the snapshotting are easy to get wrong later:

- **Enum fields store labels, not codes.** `transparency_snapshot` holds
  `"Transparent"`, not `"transparent"`. The document prints words; storing the
  code would mean re-resolving it through a mapping that may itself have
  changed.
- **Specific gravity is text, not a decimal.** It is printed verbatim and never
  used in arithmetic, and lab notation like `2.65 – 2.70` is not a number.
- **Weight is non-null**, and carries its unit. A certificate that states a
  number without its unit states nothing.
- **People are frozen as names.** `_person()` stores `get_full_name()` at issue
  time, so a gemmologist leaving, marrying or being deleted cannot change what a
  certificate says it was signed by. There are two: `gemmologist` from the
  report's `identified_by`, and `gemmologist_two` from its `verified_by`.

Instruments are stored as a JSON list of `{name, used}` rather than rows,
because they are words on a document rather than a relation anyone queries. It
covers **every** instrument the lab had at issue, not just the ones used - the
certificate prints the full checklist with a tick against each one used. Freezing
the list means adding an instrument later cannot add an unticked row to an old
document, and an inactive instrument still appears when the report used it: the
tick is a fact about the stone. No reading is recorded, only whether it was used.

## Issuing

`issue_certificate()` in `services/certificate.py` is the only way a certificate
comes into existence. It refuses, in this order:

1. the stone is already certified;
2. it has no identification report, or the report is not finalized;
3. its order's bill is not fully `paid`;
4. the stone has no recorded weight.

The last one looks like a technicality and is not. `weight_snapshot` is
non-null, so without the check the caller would get an `IntegrityError` from the
database. The guard turns that into the rule it actually expresses: a
certificate cannot state a weight nobody measured.

The report and the bill are reached by **traversal** (`stone.order.bill`) rather
than by importing `apps.identification` or `apps.billing`, because certificates
sit above both in the layer order.

On success the stone transitions to `certified`, and the certificate number
uses the order's year and natural-number sequence plus the stone label. For
example, a new stone can receive stored `CERT-2627-42-1`, displayed as
`CERT-26/27-42-1`. Existing padded numbers and letter labels remain unchanged.
Verification URLs keep the slash-free stored identifier.

There is no certificate revoke action. Existing records with status `revoked` remain readable and retain their watermark on PDFs and the public verification page. A correction authorized by `identification.edit_finalized_report` updates the findings snapshot on the existing certificate; its number, issuance date, issuer, and signatory names remain unchanged.

## The PDF

`render_certificate_pdf()` hands an HTML template and its context to WeasyPrint
and gets PDF bytes back. A4 landscape, three columns, one page — a port of the
document the lab already issues on paper.

WeasyPrint rather than a headless browser because the document stays editable by
anyone who can read a template, and there is no browser to install or keep
alive.

> **The cost is system libraries.** WeasyPrint needs pango, cairo and
> gdk-pixbuf. Its import is deferred until a certificate PDF is requested, so a
> missing library affects PDF rendering without preventing API startup. The package
> list is in [the backend README](../../README.md). Install them in any Dockerfile
> or CI image before running certificate PDF tests or downloads.

`certificate_context()` is deliberately split out from the render so tests can
assert on what the document *says* without parsing PDF bytes. That is the only
practical way to test the snapshot guarantee — that renaming a colour afterwards
does not rewrite the document.

PDFs are **rendered on demand and never stored**. The body is frozen snapshots,
so re-rendering is deterministic. Historical records marked revoked keep their
watermark and gain a `-revoked` filename suffix when downloaded.

Lab identity on the page — ministry, lab name, address — comes from
`CERTIFICATE_MINISTRY_NAME`, `CERTIFICATE_LAB_NAME` and
`CERTIFICATE_LAB_ADDRESS` in settings, not from the database.

## Images, and why everything is a data URI

Every image in the document — the header banner, the stamp, the stone photograph,
the QR code — is embedded as a base64 `data:` URI. Nothing is a path or a URL.

This is why `write_pdf()` is called with **no `base_url`**: there is nothing
left to resolve against the filesystem. The document renders identically from a
web request, a management command, a test or a background job, and never depends
on `collectstatic` having run. The cost is a few hundred kilobytes per
certificate, which is the right trade for a file that has to be reproducible
years later.

### The lab's images

`ASSETS` in `services/assets.py` maps each name the template uses to one file in
`apps/certificates/static/certificates/img/`. The map is explicit - one name, one
file - so a misnamed file is reported as missing instead of being guessed at.

| Name | File | Where it prints |
| --- | --- | --- |
| `header_banner` | `header-banner.jpg` | Alternate full-width header, 277 × 26mm |
| `coat_of_arms` | `coat-of-arms.png` | Clean header, left side |
| `tgc_logo` | `tgc-logo.png` | Clean header, right side |
| `official_stamp` | `official-stamp.png` | The stamp box in column 1 |

The default clean header embeds the coat of arms and TGC logo separately around
the four title lines: country, ministry, lab and report name. The original
pre-composed banner remains available as an alternate. `HEADER_STYLE` in
`services/pdf.py` is hard-coded to `"clean"`; changing it to `"banner"` switches
the PDF to the original image.

A missing file returns `None` with a one-time warning rather than raising, so a
certificate still renders:

- no stamp - the box is left empty, since it is where the lab stamps by hand;
- no logo - its position is left clear while all four title lines remain
  visible;
- no alternate banner - the clean header remains in use.

Found files are cached for the life of the process, so replacing one needs a
restart (or `forget_assets()`); a missing file is re-checked on every render.
Sizing and format guidance is in that directory's own `README.md`.

### The stone photograph

`photo_snapshot` holds the **stored path the stone's photo had at the moment of
issue**. Re-photographing a stone writes a new file and repoints the stone,
leaving this one untouched — so the certificate keeps its picture without
duplicating the bytes.

`photo_data_uri()` prepares it for the page, and the order of operations matters:

1. **Read through the storage backend**, not `.path` — which raises
   `NotImplementedError` the day media moves to remote storage. An unreadable
   file logs a warning and returns `None`, so the template falls back to its
   placeholder rather than the endpoint returning a 500.
2. **`ImageOps.exif_transpose` first.** A phone writes the sensor's pixels plus
   a "rotate me" tag, and re-encoding drops the tag. Without this step a
   portrait photograph prints on its side.
3. **Flatten to RGB** if needed — a PNG or HEIC can carry transparency and JPEG
   has no alpha channel to put it in.
4. **Downscale to 900px** on the long edge. The box prints at about 40mm, which
   is roughly 470px at 300dpi, so 900 leaves print headroom while cutting a
   4000px phone photograph down. Skipping this turned a 250KB document into a
   1.9MB one.
5. **Re-encode as JPEG** at quality 85.

If Pillow cannot handle the file at all, the original bytes are embedded as
uploaded — a photograph that prints imperfectly beats a certificate with no
photograph. The upload on disk is never modified.

### The QR code

`qr_data_uri()` uses `ERROR_CORRECT_M` rather than the library default of `L`. A
certificate is handled, folded and photocopied; M tolerates roughly 15% damage
against L's 7%. The payload is the verification URL below.

## Public verification

```
GET /verify/{certificate_number}/
```

Mounted in `config/urls.py` **outside** `/api/v1/`, as a plain Django `View`
rather than a DRF one. Three reasons, and each is load-bearing:

1. **The URL is printed on a physical document.** It may be followed in ten
   years, long after `/api/v2/` exists. A versioned prefix would strand every
   certificate already in circulation.
2. **It is unauthenticated by design**, and DRF's `DEFAULT_PERMISSION_CLASSES`
   fails closed. Reaching around that setting per view is exactly the drift it
   exists to prevent, so this route stays outside DRF entirely.
3. **It answers HTML for a phone camera**, not JSON.

Responses:

| Case | Status | Body |
| --- | --- | --- |
| Known, valid | 200 | The findings |
| Known, revoked | **200** | The findings, with a loud banner |
| Unknown number | 404 | A plain page, not a stack trace |

A revoked certificate answers **200** deliberately: it is a successful lookup of
a document that no longer stands, not a failed lookup.

> **It never shows the customer.** The printed certificate names no owner, and a
> public endpoint keyed on a number found on a piece of paper must not become a
> way to look people up. The findings describe the stone and identify nobody.

`CERTIFICATE_VERIFY_BASE_URL` decides the host the printed link points at. Set
it in any deployment where the API is not what the public reaches.

## The API

Registered under `/api/v1/certificates/`:

| Endpoint | Permission |
| --- | --- |
| `POST /certificates/` | `certificates.issue_certificate` |
| `GET /certificates/worklist/` | stones with a finalized report and a paid bill, not yet certified |
| `GET /certificates/{id}/pdf/` | `certificates.view_certificate` *(method map)* |

Issuing takes only the stone id — every other value is snapshotted by the
service, not supplied by the client.

`pdf` is deliberately **absent** from `action_permissions`, so it falls back to
the method map and resolves to `view_certificate`. A download is a read of data
the detail endpoint already returns; bespoke permissions are reserved for verbs
that change state.

## Report numbers

The number printed as REPORT NO is not the certificate number. It is created
when the identification report is saved by `reference_number_for_order()` in
`apps/core/services.py`, using the order's sequence and the stone's label:

```
TGC-2627-42-1
    └─┬┘ └┬┘ └ stone label
      │   sequence
      financial year 2026/2027
```

The Tanzanian financial year runs **July to June**, so August 2026 and March
2027 both fall in `2026/2027`. `financial_year()` answers that question.

The sequence restarts each financial year and is allocated once for the order;
bills, reports and certificates reuse it with their own prefix. New sequences
are unpadded (`42`), while old stored sequences retain their original padding
(`00042`). `format_reference_number()` preserves the stored sequence and adds
the slash only for display.

New orders use numeric stone labels (`1`, `2`, …). Existing orders that already
have letter-labeled stones keep those labels; newly created reports and
certificates for those stones retain the letter suffix. For a legacy order that
continues past `Z`, labels advance as `AA`, `AB`, …. Existing orders, reports,
and certificates are never rewritten by the numbering change.

`OrderSequence` stores the last sequence for each financial year. Its row lock
serializes concurrent order creation; when a year's counter is first created,
it starts after the largest existing order reference, including soft-deleted
orders. The unique order-reference constraint remains a final duplicate guard.

The value is snapshotted onto the certificate as `report_number_snapshot` at
issue time, like everything else on the page.

## Reading the code

| File | Holds |
| --- | --- |
| `models/certificate.py` | The snapshot columns, and why each one is separate |
| `services/certificate.py` | Issuing certificates and refreshing findings snapshots |
| `services/pdf.py` | The template context and the render |
| `services/assets.py` | Data URIs, the photo pipeline, the QR code |
| `verify.py` / `urls_public.py` | The public page, and why it sits outside the API |
| `templates/certificates/` | The document itself, and the verification page |
