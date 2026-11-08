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
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from apps.core.exceptions import ServiceError

from .definitions import EXPORT_ROW_LIMIT
from .selectors import report_columns, report_rows


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
    sheet["A1"].font = Font(size=16, bold=True)
    sheet.append(["Period", f"{report['range']['from']} to {report['range']['to']}"])
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
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=40,
        rightMargin=40,
        topMargin=36,
        bottomMargin=36,
    )
    styles = getSampleStyleSheet()
    value_style = ParagraphStyle(
        "HighlightValue", parent=styles["BodyText"], alignment=TA_RIGHT
    )
    heading_style = ParagraphStyle(
        "HighlightSection", parent=styles["BodyText"], fontName="Helvetica-Bold"
    )
    story = [
        Paragraph(escape(report["title"] + " highlights"), styles["Title"]),
        Paragraph(
            f"Period: {escape(report['range']['from'])} to "
            f"{escape(report['range']['to'])}",
            styles["Normal"],
        ),
    ]
    for label, value in report["applied_filters"].items():
        story.append(
            Paragraph(f"{escape(label)}: {escape(str(value))}", styles["Normal"])
        )
    story.append(Spacer(1, 12))
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


def _detail_total_rows(key, totals):
    """Keep currencies separate, including the three outstanding-bill amounts."""
    if key == "orders" and "stones" in totals:
        yield {"event_date": "Total submitted stones", "stones": totals["stones"]}
    elif key != "orders":
        for currency, amounts in sorted(totals.items()):
            yield {"event_date": f"Total ({currency})", "currency": currency, **amounts}


def _xlsx(sections):
    workbook = Workbook(write_only=True)
    for key, section in sections.items():
        summary = section["summary"]
        sheet = workbook.create_sheet(summary["title"][:31])
        columns = report_columns(key)
        sheet.freeze_panes = "A2"
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
            2,
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
        for total in _detail_total_rows(key, totals):
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


def _pdf(sections):
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        leftMargin=28,
        rightMargin=28,
        topMargin=28,
        bottomMargin=28,
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
        for total in _detail_total_rows(key, totals):
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
    return _xlsx(sections) if file_type == "xlsx" else _pdf(sections)
