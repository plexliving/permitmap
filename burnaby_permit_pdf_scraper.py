#!/usr/bin/env python3
"""Parse City of Burnaby permit-issued PDF reports for a month.

Burnaby publishes daily "Building Permits Issued & Tabulation Reports" as
PDF files. This script discovers the official PDFs for a requested month,
downloads them, extracts the embedded text, and turns each permit block into a
structured record.

It intentionally uses only the Python standard library. The Burnaby report
PDFs are generated with simple positioned text commands, so a small extractor is
enough for these reports and avoids requiring poppler, pypdf, or pdfminer.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


REPORTS_PAGE = (
    "https://www.burnaby.ca/services-and-payments/permits-and-applications/"
    "building-permits-issued-and-tabulation-reports"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
)

MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

SIZE_LABELS = {
    2: "duplex",
    3: "triplex",
    4: "fourplex",
    5: "fiveplex",
    6: "sixplex",
    7: "sevenplex",
    8: "eightplex",
}

SIZE_PATTERNS = {
    2: [r"\bduplex\b", r"\b2[\s-]?plex\b", r"\btwo[\s-]?plex\b"],
    3: [r"\btriplex\b", r"\b3[\s-]?plex\b", r"\bthree[\s-]?plex\b"],
    4: [r"\bfourplex\b", r"\b4[\s-]?plex\b", r"\bfour[\s-]?plex\b", r"\bquadplex\b"],
    5: [r"\bfiveplex\b", r"\b5[\s-]?plex\b", r"\bfive[\s-]?plex\b"],
    6: [r"\bsixplex\b", r"\b6[\s-]?plex\b", r"\bsix[\s-]?plex\b"],
    7: [r"\bsevenplex\b", r"\b7[\s-]?plex\b", r"\bseven[\s-]?plex\b"],
    8: [r"\beightplex\b", r"\b8[\s-]?plex\b", r"\beight[\s-]?plex\b"],
}

LABELS = {
    "Site Address",
    "Legal Description",
    "Current / Underlying Zone",
    "Permit",
    "Number",
    "Permit Category",
    "Type of Change",
    "Value of Work",
    "Number of Units",
    "Applicant Name",
    "Contractor Name",
    "Contractor Address",
    "Description",
}


@dataclass
class TextItem:
    text: str
    x: float
    y: float
    order: int


@dataclass
class BurnabyPermitRecord:
    permit_id: str | None
    permit_number: str | None
    issued_date: str
    address: str
    legal_description: str | None
    zoning: str | None
    permit_category: str | None
    type_of_change: str | None
    project_value: float | None
    project_value_text: str | None
    number_of_units: int | None
    applicant: str | None
    contractor: str | None
    contractor_address: str | None
    description: str | None
    size: int | None
    size_label: str | None
    matched_pattern: str | None
    match_context: str | None
    source: str
    source_pdf: str
    source_pdf_url: str


@dataclass
class TabulationSummary:
    report_month: str
    report_url: str
    two_family_current_month_permits: int | None
    multiplex_current_month_units: int | None
    parse_status: str
    notes: str | None
    source: str = "burnaby_tabulation_pdf"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    return value.strip()


def request_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def strip_tags(value: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", value))


def month_section(html_text: str, year: int, month: int) -> str | None:
    month_name = MONTH_NAMES[month]
    heading_pattern = re.compile(
        rf"<h[2-6][^>]*>\s*{re.escape(month_name)}\s+{year}\s*</h[2-6]>",
        re.I,
    )
    match = heading_pattern.search(html_text)
    if not match:
        # The web-to-text view renders these as "#### Month Year"; keep a
        # tolerant fallback in case Burnaby changes heading markup.
        match = re.search(rf"{re.escape(month_name)}\s+{year}", html_text, re.I)
    if not match:
        return None
    next_heading = re.search(r"<h[2-6][^>]*>", html_text[match.end() :], re.I)
    end = match.end() + next_heading.start() if next_heading else len(html_text)
    return html_text[match.end() : end]


def parse_day_from_daily_link(text: str, url: str, month: int, year: int) -> int | None:
    month_name = MONTH_NAMES[month]
    aliases = {month_name.lower(), month_name[:3].lower()}
    if month_name == "April":
        aliases.add("aprl")  # typo currently present on Burnaby's page
    candidates = [text, urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)]
    for candidate in candidates:
        candidate = clean_text(candidate)
        for alias in aliases:
            pattern = rf"\b{re.escape(alias)}[^\d]+(\d{{1,2}})[^\d]+{year}\b"
            match = re.search(pattern, candidate, re.I)
            if match:
                day = int(match.group(1))
                try:
                    dt.date(year, month, day)
                    return day
                except ValueError:
                    return None
    return None


def discover_daily_pdf_urls(year: int, month: int) -> list[tuple[dt.date, str]]:
    html_text = request_bytes(REPORTS_PAGE).decode("utf-8", errors="replace")
    section = month_section(html_text, year, month)
    if not section:
        return []
    found: dict[dt.date, str] = {}

    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', section, re.I | re.S):
        href = html.unescape(match.group(1))
        link_text = strip_tags(match.group(2))
        url = urllib.parse.urljoin(REPORTS_PAGE, href)
        if "tabulation" in urllib.parse.unquote(url).lower():
            continue
        day = parse_day_from_daily_link(link_text, url, month, year)
        if day is None:
            continue
        found[dt.date(year, month, day)] = url

    return sorted(found.items(), key=lambda item: item[0])


def discover_tabulation_report_url(year: int, month: int) -> str | None:
    html_text = request_bytes(REPORTS_PAGE).decode("utf-8", errors="replace")
    start = re.search(r"<h[2-6][^>]*>\s*Tabulation Reports\s*</h[2-6]>", html_text, re.I)
    if not start:
        return None
    end_match = re.search(r"<h[2-6][^>]*>", html_text[start.end() :], re.I)
    end = start.end() + end_match.start() if end_match else len(html_text)
    section = html_text[start.end() : end]
    month_name = MONTH_NAMES[month]
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', section, re.I | re.S):
        href = html.unescape(match.group(1))
        link_text = strip_tags(match.group(2))
        url = urllib.parse.urljoin(REPORTS_PAGE, href)
        haystack = f"{link_text} {urllib.parse.unquote(url)}"
        if re.search(rf"\b{re.escape(month_name)}\s+{year}\b", haystack, re.I):
            return url
    return None


def cache_pdf(url: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    parsed = urllib.parse.urlparse(url)
    filename = Path(urllib.parse.unquote(parsed.path)).name
    target = cache_dir / filename
    if not target.exists() or target.stat().st_size == 0:
        target.write_bytes(request_bytes(url))
    return target


def pdf_streams(pdf_bytes: bytes) -> Iterable[bytes]:
    for match in re.finditer(rb"stream\r?\n", pdf_bytes):
        start = match.end()
        end = pdf_bytes.find(b"endstream", start)
        if end < 0:
            continue
        raw = pdf_bytes[start:end].strip(b"\r\n")
        try:
            yield zlib.decompress(raw)
        except zlib.error:
            continue


def parse_pdf_literal(data: bytes, start: int) -> tuple[str, int]:
    assert data[start : start + 1] == b"("
    out = bytearray()
    depth = 1
    i = start + 1
    while i < len(data) and depth:
        ch = data[i]
        if ch == 92:  # backslash
            i += 1
            if i >= len(data):
                break
            esc = data[i]
            escapes = {
                ord("n"): ord("\n"),
                ord("r"): ord("\r"),
                ord("t"): ord("\t"),
                ord("b"): 8,
                ord("f"): 12,
                ord("("): ord("("),
                ord(")"): ord(")"),
                ord("\\"): ord("\\"),
            }
            if esc in escapes:
                out.append(escapes[esc])
            elif 48 <= esc <= 55:
                octal = bytes([esc])
                for _ in range(2):
                    if i + 1 < len(data) and 48 <= data[i + 1] <= 55:
                        i += 1
                        octal += bytes([data[i]])
                    else:
                        break
                out.append(int(octal, 8))
            elif esc in {10, 13}:
                if esc == 13 and i + 1 < len(data) and data[i + 1] == 10:
                    i += 1
            else:
                out.append(esc)
        elif ch == ord("("):
            depth += 1
            out.append(ch)
        elif ch == ord(")"):
            depth -= 1
            if depth:
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    return out.decode("latin-1", errors="replace"), i


def parse_text_items(pdf_bytes: bytes) -> list[TextItem]:
    items: list[TextItem] = []
    order = 0
    block_pattern = re.compile(rb"BT(.*?)ET", re.S)
    td_pattern = re.compile(rb"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+Td\b")

    for stream in pdf_streams(pdf_bytes):
        for block_match in block_pattern.finditer(stream):
            block = block_match.group(1)
            x = y = 0.0
            pos = 0
            while pos < len(block):
                td = td_pattern.search(block, pos)
                literal_pos = block.find(b"(", pos)
                if td and (literal_pos < 0 or td.start() < literal_pos):
                    x = float(td.group(1))
                    y = float(td.group(2))
                    pos = td.end()
                    continue
                if literal_pos < 0:
                    break
                text, end = parse_pdf_literal(block, literal_pos)
                after = block[end : end + 12]
                if re.match(rb"\s*Tj\b", after):
                    text = clean_text(text)
                    if text:
                        items.append(TextItem(text=text, x=x, y=y, order=order))
                        order += 1
                pos = end + 1
    return items


def is_label(text: str) -> bool:
    return text in LABELS


def join_parts(parts: Iterable[str]) -> str | None:
    cleaned = [clean_text(part) for part in parts if clean_text(part)]
    if not cleaned:
        return None
    return clean_text(" ".join(cleaned))


def parse_money(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_int(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def parse_total_units(text: str | None) -> int | None:
    if not text:
        return None
    patterns = [
        r"\btotal\s+units?\s*[:=]?\s*(\d+)\b",
        r"\((?:[^)]*?)total\s+units?\s*[:=]?\s*(\d+)(?:[^)]*?)\)",
        r"\b(\d+)\s+(?:dwelling|residential|rental)\s+units?\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return int(match.group(1))
    return None


def parse_issued_date_from_items(items: list[TextItem], fallback: dt.date) -> dt.date:
    for idx, item in enumerate(items):
        if item.text != "Permits Issued On:":
            continue
        for next_item in items[idx + 1 : idx + 4]:
            try:
                return dt.datetime.strptime(next_item.text, "%B %d, %Y").date()
            except ValueError:
                continue
    return fallback


def split_category_type(parts: list[str]) -> tuple[str | None, str | None]:
    cleaned = [p for p in (clean_text(part) for part in parts) if p and not is_label(p)]
    if not cleaned:
        return None, None
    category_parts: list[str] = []
    type_parts: list[str] = []
    for part in cleaned:
        if not type_parts and (
            part.startswith("(")
            or re.search(r"\bPermit\b", part)
            or part in {"Building Permit", "Demolition Permit", "Plumbing Permit"}
        ):
            category_parts.append(part)
        else:
            type_parts.append(part)
    if not category_parts and cleaned:
        category_parts = cleaned[:1]
        type_parts = cleaned[1:]
    return join_parts(category_parts), join_parts(type_parts)


def split_legal_zone(parts: list[str]) -> tuple[str | None, str | None]:
    cleaned = [p for p in (clean_text(part) for part in parts) if p and not is_label(p)]
    if not cleaned:
        return None, None
    zone = cleaned[-1] if re.fullmatch(r"[A-Z]{1,4}\d?(?:[-/]\d+)?|CD|RM\d+|R\d+|M\d+", cleaned[-1]) else None
    legal = cleaned[:-1] if zone else cleaned
    return join_parts(legal), zone


def classify_size(record_text: str, number_of_units: int | None) -> tuple[int | None, str | None, str | None, str | None]:
    lowered = record_text.lower()
    for size, patterns in SIZE_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, lowered, re.I)
            if match:
                return size, SIZE_LABELS[size], pattern, record_text[max(0, match.start() - 90) : match.end() + 90]
    total_units = parse_total_units(record_text)
    if total_units in SIZE_LABELS:
        return total_units, SIZE_LABELS[total_units], "total_units_in_description", record_text[:220]
    if number_of_units in SIZE_LABELS:
        return number_of_units, SIZE_LABELS[number_of_units], "number_of_units", record_text[:220]
    return None, None, None, None


def infer_address_from_text(text: str | None) -> str | None:
    if not text:
        return None
    patterns = [
        r"\b(?:\d+\s*-\s*)?\d{3,5}\s+[A-Z0-9' .-]+(?:ST|AVE|AVENUE|RD|ROAD|DR|DRIVE|WAY|BLVD|CRES|CRT|COURT|PL|PLACE|LANE|HWY|HIGHWAY|PROM|PARKWAY)\b",
        r"\bS\d+\s*-\s*\d{3,5}\s+[A-Z0-9' .-]+(?:ST|AVE|AVENUE|RD|ROAD|DR|DRIVE|WAY|BLVD|CRES|CRT|COURT|PL|PLACE|LANE|HWY|HIGHWAY|PROM|PARKWAY)\b",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.I)
        if matches:
            return clean_text(matches[-1])
    return None


def infer_permit_number_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r"\b[A-Z]+(?:\d{2})?-\d{5}\b", text)
    return match.group(0) if match else None


def next_index(items: list[TextItem], start: int, labels: set[str]) -> int | None:
    for idx in range(start, len(items)):
        if items[idx].text in labels:
            return idx
    return None


def previous_index(items: list[TextItem], start: int, labels: set[str]) -> int | None:
    for idx in range(start, -1, -1):
        if items[idx].text in labels:
            return idx
    return None


def parse_record_from_bounds(
    items: list[TextItem],
    start_idx: int,
    end_idx: int,
    issued_date: dt.date,
    pdf_path: Path,
    pdf_url: str,
) -> BurnabyPermitRecord | None:
    segment = items[start_idx:end_idx]
    if not segment:
        return None
    permit_item = next(
        (item for item in segment if re.fullmatch(r"[A-Z]+(?:\d{2})?-\d{5}", item.text)),
        None,
    )
    applicant_idx = next_index(items, start_idx, {"Applicant Name"})
    contractor_idx = next_index(items, applicant_idx + 1, {"Contractor Name"})
    description_idx = next_index(items, (contractor_idx if contractor_idx is not None else applicant_idx) + 1, {"Description"})
    if applicant_idx is None or applicant_idx >= end_idx:
        return None

    before_applicant = [item for item in items[start_idx:applicant_idx] if not is_label(item.text)]
    address = join_parts(item.text for item in before_applicant if 45 <= item.x < 160)
    legal, zone = split_legal_zone([item.text for item in before_applicant if 160 <= item.x < 312])
    category, type_of_change = split_category_type([item.text for item in before_applicant if 395 <= item.x < 498])
    value_unit_parts = [item.text for item in before_applicant if item.x >= 498]
    project_value_text = next((part for part in value_unit_parts if "$" in part), None)
    unit_text = next((part for part in value_unit_parts if "$" not in part), None)

    applicant_end = min(contractor_idx or description_idx or end_idx, end_idx)
    applicant = join_parts(item.text for item in items[applicant_idx + 1 : applicant_end] if not is_label(item.text))

    contractor_items: list[TextItem] = []
    if contractor_idx is not None and contractor_idx < end_idx:
        contractor_end = min(description_idx or end_idx, end_idx)
        contractor_items = [item for item in items[contractor_idx + 1 : contractor_end] if not is_label(item.text)]
    contractor = join_parts(item.text for item in contractor_items if item.x < 280)
    contractor_address = join_parts(item.text for item in contractor_items if item.x >= 280)

    description = None
    if description_idx is not None and description_idx < end_idx:
        desc_end = end_idx
        for idx in range(description_idx + 1, len(items)):
            if idx >= end_idx or items[idx].text in {"Site Address", "Legal Description", "Permits Issued On:"}:
                desc_end = idx
                break
        description = join_parts(item.text for item in items[description_idx + 1 : desc_end] if not is_label(item.text))

    record_text = " ".join(
        part
        for part in [
            permit_item.text if permit_item else "",
            address or "",
            category or "",
            type_of_change or "",
            applicant or "",
            contractor or "",
            description or "",
        ]
        if part
    )
    inferred_permit = permit_item.text if permit_item else infer_permit_number_from_text(description or record_text)
    inferred_address = address or infer_address_from_text(description or record_text) or ""
    number_of_units = parse_int(unit_text)
    size, size_label, matched_pattern, match_context = classify_size(record_text, number_of_units)

    if size_label == "duplex" and description:
        lowered_desc = description.lower()
        if "commercial" in lowered_desc or "demolition permit" in lowered_desc:
            return None

    return BurnabyPermitRecord(
        permit_id=inferred_permit,
        permit_number=inferred_permit,
        issued_date=issued_date.isoformat(),
        address=inferred_address,
        legal_description=legal,
        zoning=zone,
        permit_category=category,
        type_of_change=type_of_change,
        project_value=parse_money(project_value_text),
        project_value_text=project_value_text,
        number_of_units=number_of_units,
        applicant=applicant,
        contractor=contractor,
        contractor_address=contractor_address,
        description=description,
        size=size,
        size_label=size_label,
        matched_pattern=matched_pattern,
        match_context=match_context,
        source="burnaby_permit_pdf",
        source_pdf=str(pdf_path),
        source_pdf_url=pdf_url,
    )


def parse_pdf(pdf_path: Path, issued_date: dt.date, pdf_url: str) -> list[BurnabyPermitRecord]:
    items = parse_text_items(pdf_path.read_bytes())
    issued_date = parse_issued_date_from_items(items, issued_date)
    records: list[BurnabyPermitRecord] = []
    starts = [idx for idx, item in enumerate(items) if item.text == "Site Address"]
    for pos, start_idx in enumerate(starts):
        end_idx = starts[pos + 1] if pos + 1 < len(starts) else len(items)
        record = parse_record_from_bounds(items, start_idx, end_idx, issued_date, pdf_path, pdf_url)
        if record:
            records.append(record)

    seen = {record.permit_number for record in records if record.permit_number}
    # Some Burnaby PDFs omit "Site Address" extraction for a block but still
    # expose the permit number. Parse those as a fallback without duplicating
    # normal section-based records.
    permit_indexes = [
        idx
        for idx, item in enumerate(items)
        if re.fullmatch(r"[A-Z]+(?:\d{2})?-\d{5}", item.text)
        and item.text not in seen
    ]
    for permit_idx in permit_indexes:
        start_idx = previous_index(items, permit_idx, {"Site Address", "Legal Description"}) or permit_idx
        end_idx = next_index(items, permit_idx + 1, {"Site Address"}) or len(items)
        record = parse_record_from_bounds(items, start_idx, end_idx, issued_date, pdf_path, pdf_url)
        if record:
            records.append(record)
    return records


def parse_tabulation_summary(pdf_path: Path, report_month: str, report_url: str, month: int) -> TabulationSummary:
    items = parse_text_items(pdf_path.read_bytes())
    text = " ".join(item.text for item in items)
    text = re.sub(r"\s+", " ", text)

    two_family = None
    match = re.search(r"\bTwo Family\s+(\d+)\s+\$", text, re.I)
    if match:
        two_family = int(match.group(1))

    multiplex_units = None
    match = re.search(r"\bMultiplex\s+((?:\d+\s+){11,13}\d+)\b", text, re.I)
    if match:
        values = [int(value) for value in match.group(1).split()]
        if len(values) >= month:
            multiplex_units = values[month - 1]

    parse_status = "parsed" if two_family is not None or multiplex_units is not None else "not_parsed"
    notes = None
    if not items:
        notes = (
            "Tabulation PDF uses an encoded font layout that the built-in daily-report "
            "text extractor cannot decode; URL retained for manual aggregate verification."
        )
    elif parse_status == "not_parsed":
        notes = "Tabulation PDF text was extracted but expected aggregate labels were not found."

    return TabulationSummary(
        report_month=report_month,
        report_url=report_url,
        two_family_current_month_permits=two_family,
        multiplex_current_month_units=multiplex_units,
        parse_status=parse_status,
        notes=notes,
    )


def parse_size_filter(value: str) -> set[int] | None:
    value = value.strip().lower()
    if value in {"all", "*", ""}:
        return None
    sizes: set[int] = set()
    for part in re.split(r"[, ]+", value):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            sizes.update(range(int(left), int(right) + 1))
        else:
            sizes.add(int(part))
    bad = sizes - set(SIZE_LABELS)
    if bad:
        raise argparse.ArgumentTypeError(f"unsupported sizes: {sorted(bad)}")
    return sizes


def default_output_path(year: int, month: int, output_format: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"burnaby_permits_{year}_{month:02d}_{stamp}.{output_format}"


def write_output(records: list[BurnabyPermitRecord], output_format: str, output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        path.write_text(json.dumps([asdict(record) for record in records], indent=2), encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(BurnabyPermitRecord.__dataclass_fields__.keys())
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse official City of Burnaby daily permit PDF reports for a month."
    )
    parser.add_argument("--year", type=int, required=True, help="Report year, e.g. 2026")
    parser.add_argument("--month", type=int, required=True, choices=range(1, 13), help="Report month number")
    parser.add_argument(
        "--sizes",
        default="all",
        help="Optional multiplex size filter such as 2, 2-4, or all. Uses number of units and description text.",
    )
    parser.add_argument(
        "--multiplex-only",
        action="store_true",
        help="Only output records classified as duplex through eightplex.",
    )
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--output", help="Output file path. Defaults to a timestamped file.")
    parser.add_argument(
        "--cache-dir",
        default=".cache/burnaby_permit_pdfs",
        help="Directory for downloaded official PDF reports.",
    )
    parser.add_argument(
        "--pdf",
        action="append",
        help="Parse a local PDF instead of discovering/downloading monthly reports. Can be repeated.",
    )
    parser.add_argument(
        "--no-tabulation-check",
        action="store_true",
        help="Skip discovering/parsing the monthly tabulation report used for aggregate verification.",
    )
    parser.add_argument(
        "--tabulation-output",
        help="Optional path for the parsed monthly tabulation summary JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    size_filter = parse_size_filter(args.sizes)
    cache_dir = Path(args.cache_dir)

    pdfs: list[tuple[dt.date, Path, str]] = []
    tabulation_summary: TabulationSummary | None = None
    if args.pdf:
        issued = dt.date(args.year, args.month, 1)
        for pdf in args.pdf:
            path = Path(pdf)
            pdfs.append((issued, path, path.resolve().as_uri()))
    else:
        discovered = discover_daily_pdf_urls(args.year, args.month)
        if not discovered:
            print(
                f"No daily Burnaby permit-issued PDF links found in the {MONTH_NAMES[args.month]} {args.year} section.",
                file=sys.stderr,
            )
            tab_url = None if args.no_tabulation_check else discover_tabulation_report_url(args.year, args.month)
            if tab_url:
                print(
                    "A monthly tabulation report exists, but it is aggregate-only and was not used as permit records:",
                    tab_url,
                    file=sys.stderr,
                )
            return 2
        for issued_date, url in discovered:
            pdfs.append((issued_date, cache_pdf(url, cache_dir / f"{args.year}-{args.month:02d}"), url))

        if not args.no_tabulation_check:
            tab_url = discover_tabulation_report_url(args.year, args.month)
            if tab_url:
                tab_pdf = cache_pdf(tab_url, cache_dir / f"{args.year}-{args.month:02d}" / "tabulation")
                tabulation_summary = parse_tabulation_summary(
                    tab_pdf,
                    f"{MONTH_NAMES[args.month]} {args.year}",
                    tab_url,
                    args.month,
                )
                if args.tabulation_output:
                    Path(args.tabulation_output).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.tabulation_output).write_text(
                        json.dumps(asdict(tabulation_summary), indent=2),
                        encoding="utf-8",
                    )

    records: list[BurnabyPermitRecord] = []
    for issued_date, pdf_path, pdf_url in pdfs:
        records.extend(parse_pdf(pdf_path, issued_date, pdf_url))

    if args.multiplex_only:
        records = [record for record in records if record.size in SIZE_LABELS]
    if size_filter is not None:
        records = [record for record in records if record.size in size_filter]

    output = args.output or default_output_path(args.year, args.month, args.format)
    write_output(records, args.format, output)
    print(f"Wrote {len(records)} records to {output}")
    print(f"Parsed {len(pdfs)} daily permit-issued PDF report(s).")
    if tabulation_summary:
        if tabulation_summary.parse_status == "parsed":
            print(
                "Tabulation check:",
                f"Two Family permits={tabulation_summary.two_family_current_month_permits}",
                f"Multiplex units={tabulation_summary.multiplex_current_month_units}",
                f"source={tabulation_summary.report_url}",
            )
        else:
            print("Tabulation report found for manual aggregate check:", tabulation_summary.report_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
