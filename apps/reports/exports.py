"""Generate complete filtered downloads from the same projections as the API."""

from decimal import Decimal
from io import BytesIO
from math import ceil
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    LongTable,
    PageTemplate,
    Paragraph,
    Spacer,
    TableStyle,
)

from django.conf import settings
from django.utils import timezone

from apps.certificates.services.assets import ASSET_DIR, ASSETS
from apps.core.exceptions import ServiceError

from .definitions import EXPORT_ROW_LIMIT, REPORT_TIMEZONE
from .selectors import report_columns, report_rows

PDF_BLUE = colors.HexColor("#0152a9")
PDF_NAVY = colors.HexColor("#000080")
PDF_GOLD = colors.HexColor("#e4a41e")
PDF_MUTED = colors.HexColor("#475569")


def _draw_pdf_mark(pdf_canvas, name, x, y, width, height):
    """Fit a certificate mark into its report-header box without distortion."""
    path = ASSET_DIR / ASSETS[name]
    if not path.is_file():
        return
    image = ImageReader(str(path))
    image_width, image_height = image.getSize()
    scale = min(width / image_width, height / image_height)
    drawn_width, drawn_height = image_width * scale, image_height * scale
    pdf_canvas.drawImage(
        image,
        x + (width - drawn_width) / 2,
        y + (height - drawn_height) / 2,
        width=drawn_width,
        height=drawn_height,
        mask="auto",
    )


def _draw_centered_header_line(pdf_canvas, value, y, font, size, max_width, color):
    """Shrink long configured institution names to stay between the two marks."""
    while pdfmetrics.stringWidth(value, font, size) > max_width and size > 8:
        size -= 0.5
    pdf_canvas.setFillColor(color)
    pdf_canvas.setFont(font, size)
    pdf_canvas.drawCentredString(landscape(A4)[0] / 2, y, value)


def _draw_report_pdf_page(pdf_canvas, document, report, title):
    """Show the certificate-inspired masthead once and paginate the report."""
    pdf_canvas.saveState()
    if document.page != 1:
        _draw_report_pdf_footer(pdf_canvas, document, report)
        pdf_canvas.restoreState()
        return
    page_width, page_height = landscape(A4)
    left, right = document.leftMargin, page_width - document.rightMargin
    _draw_pdf_mark(pdf_canvas, "coat_of_arms", left + 3, page_height - 101, 58, 70)
    _draw_pdf_mark(pdf_canvas, "tgc_logo", right - 61, page_height - 101, 58, 70)
    text_width = right - left - 150
    _draw_centered_header_line(
        pdf_canvas,
        "United Republic of Tanzania",
        page_height - 34,
        "Helvetica-Bold",
        11,
        text_width,
        PDF_NAVY,
    )
    _draw_centered_header_line(
        pdf_canvas,
        settings.CERTIFICATE_MINISTRY_NAME,
        page_height - 50,
        "Helvetica-Bold",
        12,
        text_width,
        PDF_NAVY,
    )
    _draw_centered_header_line(
        pdf_canvas,
        settings.CERTIFICATE_LAB_NAME,
        page_height - 69,
        "Helvetica-Bold",
        14,
        text_width,
        PDF_BLUE,
    )
    _draw_centered_header_line(
        pdf_canvas,
        title,
        page_height - 94,
        "Helvetica-Bold",
        15,
        text_width,
        PDF_NAVY,
    )
    pdf_canvas.setStrokeColor(PDF_GOLD)
    pdf_canvas.setLineWidth(2)
    pdf_canvas.line(left, page_height - 104, right, page_height - 104)

    band_y = page_height - 164
    pdf_canvas.setFillColor(colors.HexColor("#f1f5f9"))
    pdf_canvas.roundRect(left, band_y, right - left, 54, 4, stroke=0, fill=1)
    pdf_canvas.setFillColor(PDF_NAVY)
    pdf_canvas.setFont("Helvetica-Bold", 9)
    pdf_canvas.drawString(left + 10, band_y + 38, "DATE RANGE")
    pdf_canvas.setFont("Helvetica", 10)
    pdf_canvas.drawString(
        left + 95,
        band_y + 37,
        f"{report['range']['from']} to {report['range']['to']}",
    )
    filter_text = (
        ", ".join(
            f"{label}: {value}" for label, value in report["applied_filters"].items()
        )
        or "All records"
    )
    filters = Paragraph(
        f"<b>FILTERS</b> &nbsp;&nbsp; {escape(filter_text)}",
        ParagraphStyle(
            "ReportHeaderFilters",
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=PDF_MUTED,
        ),
    )
    filters.wrap(right - left - 20, 25)
    filters.drawOn(pdf_canvas, left + 10, band_y + 5)

    _draw_report_pdf_footer(pdf_canvas, document, report)
    pdf_canvas.restoreState()


def _draw_report_pdf_footer(pdf_canvas, document, report):
    """Keep generation details and page numbers on every page."""
    page_width, _ = landscape(A4)
    left, right = document.leftMargin, page_width - document.rightMargin
    pdf_canvas.setStrokeColor(colors.HexColor("#cbd5e1"))
    pdf_canvas.setLineWidth(0.5)
    pdf_canvas.line(left, 33, right, 33)
    pdf_canvas.setFillColor(PDF_MUTED)
    pdf_canvas.setFont("Helvetica", 8)
    generated_at = report.get("generated_at")
    if generated_at:
        generated = timezone.localtime(generated_at, REPORT_TIMEZONE).strftime(
            "%Y-%m-%d %H:%M"
        )
        pdf_canvas.drawString(left, 21, f"Generated {generated} EAT")
    else:
        pdf_canvas.drawString(left, 21, settings.CERTIFICATE_LAB_NAME)
    pdf_canvas.drawRightString(right, 21, f"Page {document.page}")


def _report_pdf_document(output, report, title, *, side_margin):
    """Use a tall first-page header and a compact frame on later pages."""
    page_width, page_height = landscape(A4)
    document = BaseDocTemplate(
        output,
        pagesize=(page_width, page_height),
        leftMargin=side_margin,
        rightMargin=side_margin,
        topMargin=181,
        bottomMargin=45,
    )

    def decorate(pdf_canvas, doc):
        _draw_report_pdf_page(pdf_canvas, doc, report, title)

    first_frame = Frame(
        side_margin,
        document.bottomMargin,
        document.width,
        document.height,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    later_frame = Frame(
        side_margin,
        document.bottomMargin,
        document.width,
        page_height - 40 - document.bottomMargin,
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    document.addPageTemplates(
        [
            PageTemplate(
                id="First",
                frames=[first_frame],
                onPage=decorate,
                autoNextPageTemplate="Later",
            ),
            PageTemplate(id="Later", frames=[later_frame], onPage=decorate),
        ]
    )
    return document


def _highlight_sections(sections):
    """Give both file formats the same simple, permission scoped summary rows."""
    for section in sections.values():
        summary = section["summary"]
        rows = [("Count", summary["count"])]
        for amount in summary["amounts"]:
            rows.append((f"Amount ({amount['currency']})", Decimal(amount["amount"])))
        for status in summary["statuses"]:
            rows.append(
                (status["status"].replace("_", " ").capitalize(), status["count"])
            )
        for heading, groups in (
            ("Stone categories", summary["categories"]),
            ("Varieties", summary["varieties"]),
        ):
            visible_groups = [
                group
                for group in groups
                if heading != "Stone categories" or group["name"] != "TestCategory"
            ]
            if visible_groups:
                rows.append((f"{heading} (count)", None))
            for group in visible_groups:
                name = group["name"] or "Unspecified"
                rows.append((name, group["count"]))
                for amount in group["amounts"]:
                    rows.append(
                        (
                            f"{name} amount ({amount['currency']})",
                            Decimal(amount["amount"]),
                        )
                    )
        if summary["missing_dates"]:
            rows.append(
                (
                    "Records without an event date (excluded)",
                    summary["missing_dates"],
                )
            )
        yield summary["title"], rows


def _highlight_xlsx(report, sections):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Highlights"
    sheet.append([report["title"] + " highlights"])
    sheet.merge_cells("A1:B1")
    sheet["A1"].font = Font(size=16, bold=True, color="24466A")
    sheet.row_dimensions[1].height = 25
    sheet.append(["Date range", f"{report['range']['from']} to {report['range']['to']}"])
    sheet["A2"].font = Font(bold=True)
    for label, value in report["applied_filters"].items():
        sheet.append([label, value])
    sheet.append([""])
    sheet.freeze_panes = f"A{sheet.max_row + 1}"
    for title, rows in _highlight_sections(sections):
        sheet.append([title, ""])
        heading_row = sheet.max_row
        for cell in sheet[heading_row][:2]:
            cell.fill = PatternFill("solid", fgColor="24466A")
        heading = sheet.cell(heading_row, 1)
        heading.font = Font(bold=True, color="FFFFFF")
        for label, value in rows:
            sheet.append([label, value])
            value_cell = sheet.cell(sheet.max_row, 2)
            value_cell.alignment = Alignment(horizontal="right")
            if isinstance(value, Decimal):
                value_cell.number_format = "#,##0.00"
            elif isinstance(value, int):
                value_cell.number_format = "#,##0"
            elif value is None:
                sheet.cell(sheet.max_row, 1).font = Font(bold=True)
        sheet.append([""])
    # Filter labels and category names may come from data; never parse them as formulas.
    for row in sheet:
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    sheet.column_dimensions["A"].width = 48
    sheet.column_dimensions["B"].width = 24
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _highlight_pdf(report, sections):
    output = BytesIO()
    document = _report_pdf_document(
        output, report, report["title"].title() + " Highlights", side_margin=40
    )
    styles = getSampleStyleSheet()
    value_style = ParagraphStyle(
        "HighlightValue", parent=styles["BodyText"], alignment=TA_RIGHT
    )
    heading_style = ParagraphStyle(
        "HighlightSection", parent=styles["BodyText"], fontName="Helvetica-Bold"
    )
    story = []
    for title, rows in _highlight_sections(sections):
        data = [[Paragraph(escape(title), heading_style), ""]]
        for label, value in rows:
            display_value = (
                f"{value:,.2f}"
                if isinstance(value, Decimal)
                else f"{value:,}"
                if isinstance(value, int)
                else ""
            )
            data.append(
                [
                    Paragraph(escape(label), styles["BodyText"]),
                    Paragraph(display_value, value_style),
                ]
            )
        table = LongTable(
            data,
            colWidths=[document.width * 0.72, document.width * 0.28],
            repeatRows=1,
        )
        table.setStyle(
            TableStyle(
                [
                    ("SPAN", (0, 0), (-1, 0)),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7eef0")),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f8fafc")],
                    ),
                    ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#d8e0e5")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(table)
        story.append(Spacer(1, 12))
    document.build(story)
    return output.getvalue()


def export_highlights(report: dict, sections: dict, *, file_type: str) -> bytes:
    """Export only the permission scoped totals shown on the highlights page."""
    return (
        _highlight_xlsx(report, sections)
        if file_type == "xlsx"
        else _highlight_pdf(report, sections)
    )


def _add_detail_totals(key, columns, totals, row):
    """Accumulate only values that have a meaningful detail-column total."""
    if key == "orders":
        totals["stones"] = totals.get("stones", 0) + int(row["stones"] or 0)
        return
    money_keys = [column["key"] for column in columns if column["kind"] == "money"]
    if money_keys:
        bucket = totals.setdefault(row["currency"], {})
        for field in money_keys:
            bucket[field] = bucket.get(field, Decimal(0)) + Decimal(row[field])


def _detail_total_rows(key, totals, count):
    """Show operational counts and keep financial amounts in TZS."""
    operational_names = {
        "orders": "orders",
        "stones": "stones",
        "certificates": "certificates",
    }
    if key in operational_names:
        row = {"event_date": f"Total {operational_names[key]}: {count}"}
        if key == "orders":
            row["stones"] = totals.get("stones", 0)
        yield row
    else:
        for currency, amounts in sorted(totals.items()):
            yield {"event_date": f"Total ({currency})", "currency": currency, **amounts}


def _xlsx(report, sections):
    workbook = Workbook(write_only=True)
    for key, section in sections.items():
        summary = section["summary"]
        sheet = workbook.create_sheet(summary["title"][:31])
        columns = report_columns(key)
        sheet.freeze_panes = "A5"
        sheet.row_dimensions[1].height = 25
        title = WriteOnlyCell(
            sheet, value=f"{report['title'].title()}: {summary['title']}"
        )
        title.font = Font(size=15, bold=True, color="24466A")
        sheet.append([title])
        date_range = WriteOnlyCell(
            sheet,
            value=f"Date range: {report['range']['from']} to {report['range']['to']}",
        )
        date_range.font = Font(size=10, color="475569")
        sheet.append([date_range])
        sheet.append([])
        widths = [
            40
            if column["key"] == "customer"
            else (24 if column["kind"] == "text" else 18)
            for column in columns
        ]
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        headers = []
        for column in columns:
            cell = WriteOnlyCell(sheet, value=column["label"])
            cell.font = Font(bold=True)
            headers.append(cell)
        sheet.append(headers)
        totals = {}
        for row_number, row in enumerate(
            report_rows(key, section["queryset"].iterator(chunk_size=500)),
            5,
        ):
            _add_detail_totals(key, columns, totals, row)
            sheet.row_dimensions[row_number].height = 15 * max(
                ceil(len(str(row[column["key"]])) / width)
                for column, width in zip(columns, widths, strict=True)
            )
            cells = []
            for column in columns:
                value = row[column["key"]]
                cell = WriteOnlyCell(sheet, value=value)
                cell.alignment = Alignment(wrap_text=True, vertical="top")
                if column["kind"] == "money":
                    cell.value = float(value)
                    cell.number_format = "#,##0.00"
                elif isinstance(value, str):
                    cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        for total in _detail_total_rows(key, totals, summary["count"]):
            cells = []
            for column in columns:
                value = total.get(column["key"], "")
                cell = WriteOnlyCell(sheet, value=value)
                cell.font = Font(bold=True)
                cell.fill = PatternFill("solid", fgColor="E7EEF0")
                cell.border = Border(top=Side(style="thin", color="9AAFB9"))
                if column["kind"] == "money" and value != "":
                    cell.value = float(value)
                    cell.number_format = "#,##0.00"
                elif isinstance(value, str):
                    cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _pdf(report, sections):
    output = BytesIO()
    document = _report_pdf_document(
        output, report, report["title"].title(), side_margin=28
    )
    styles = getSampleStyleSheet()
    styles["BodyText"].fontSize = 8
    styles["BodyText"].leading = 10
    story = []
    money_style = ParagraphStyle(
        "ReportMoney", parent=styles["BodyText"], alignment=TA_RIGHT
    )
    section_title_style = ParagraphStyle(
        "ReportSectionTitle", parent=styles["BodyText"], fontName="Helvetica-Bold"
    )
    total_style = ParagraphStyle(
        "ReportTotal", parent=styles["BodyText"], fontName="Helvetica-Bold"
    )
    total_money_style = ParagraphStyle(
        "ReportTotalMoney", parent=total_style, alignment=TA_RIGHT
    )
    for key, section in sections.items():
        summary = section["summary"]
        columns = report_columns(key)
        data = [
            [Paragraph(escape(summary["title"]), section_title_style)]
            + [""] * (len(columns) - 1),
            [
                Paragraph(escape(column["label"]), styles["BodyText"])
                for column in columns
            ],
        ]
        totals = {}
        for row in report_rows(key, section["queryset"].iterator(chunk_size=500)):
            _add_detail_totals(key, columns, totals, row)
            data.append(
                [
                    Paragraph(escape(f"{Decimal(row[column['key']]):,.2f}"), money_style)
                    if column["kind"] == "money"
                    else Paragraph(escape(str(row[column["key"]])), styles["BodyText"])
                    for column in columns
                ]
            )
        total_start = len(data)
        for total in _detail_total_rows(key, totals, summary["count"]):
            data.append(
                [
                    Paragraph(
                        escape(
                            f"{Decimal(total[column['key']]):,.2f}"
                            if column["kind"] == "money" and column["key"] in total
                            else str(total.get(column["key"], ""))
                        ),
                        total_money_style if column["kind"] == "money" else total_style,
                    )
                    for column in columns
                ]
            )
        table = LongTable(
            data,
            colWidths=[document.width / len(columns)] * len(columns),
            repeatRows=2,
        )
        table_style = [
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#e7eef0")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#ccd5d8")),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        if len(data) > total_start:
            table_style.extend(
                [
                    (
                        "BACKGROUND",
                        (0, total_start),
                        (-1, -1),
                        colors.HexColor("#e7eef0"),
                    ),
                    (
                        "LINEABOVE",
                        (0, total_start),
                        (-1, total_start),
                        1,
                        colors.HexColor("#9aafb9"),
                    ),
                ]
            )
        table.setStyle(TableStyle(table_style))
        story.append(table)
        story.append(Spacer(1, 12))
    document.build(story)
    return output.getvalue()


def export_report(report: dict, sections: dict, *, file_type: str) -> bytes:
    """Reject oversized results before fetching rows; downloads never truncate."""
    if (
        sum(section["summary"]["count"] for section in sections.values())
        > EXPORT_ROW_LIMIT
    ):
        raise ServiceError(
            f"Exports allow at most {EXPORT_ROW_LIMIT:,} rows. Narrow the report filters."
        )
    return _xlsx(report, sections) if file_type == "xlsx" else _pdf(report, sections)
