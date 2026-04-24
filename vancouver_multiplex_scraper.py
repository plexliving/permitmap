#!/usr/bin/env python3
"""Search Vancouver submitted permits for duplex to 8-plex projects.

This script uses the City of Vancouver public permit portal:
https://plposweb.vancouver.ca/Public/Default.aspx?PossePresentation=PermitSearchByDate

It submits a created-date search, opens each permit detail page, and
classifies permits by multiplex size from text on the detail page.
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
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


SEARCH_URL = (
    "https://plposweb.vancouver.ca/Public/Default.aspx"
    "?PossePresentation=PermitSearchByDate&IconName=form_yellow_search.png"
)
BASE_URL = "https://plposweb.vancouver.ca/Public/"
ISSUED_BUILDING_PERMITS_API = (
    "https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets/"
    "issued-building-permits/records"
)
BC_GEOCODER_API = "https://geocoder.api.gov.bc.ca/addresses.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
)

CREATED_DATE_FROM_COL = 972146
CREATED_DATE_TO_COL = 984849
PERMIT_TYPE_COL = 1039023


SIZE_LABELS = {
    2: "duplex",
    3: "triplex",
    4: "fourplex",
    5: "fiveplex",
    6: "sixplex",
    7: "sevenplex",
    8: "eightplex",
}

NUMBER_WORDS = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
}

SIZE_PATTERNS = {
    2: [
        r"\bduplex\b",
        r"\b2[\s-]?plex\b",
        r"\btwo[\s-]?plex\b",
        r"\btwo[\s-]?family dwelling\b",
    ],
    3: [
        r"\btriplex\b",
        r"\b3[\s-]?plex\b",
        r"\bthree[\s-]?plex\b",
    ],
    4: [
        r"\bfourplex\b",
        r"\b4[\s-]?plex\b",
        r"\bfour[\s-]?plex\b",
        r"\bquadplex\b",
    ],
    5: [
        r"\bfiveplex\b",
        r"\b5[\s-]?plex\b",
        r"\bfive[\s-]?plex\b",
    ],
    6: [
        r"\bsixplex\b",
        r"\b6[\s-]?plex\b",
        r"\bsix[\s-]?plex\b",
    ],
    7: [
        r"\bsevenplex\b",
        r"\b7[\s-]?plex\b",
        r"\bseven[\s-]?plex\b",
    ],
    8: [
        r"\beightplex\b",
        r"\b8[\s-]?plex\b",
        r"\beight[\s-]?plex\b",
    ],
}


@dataclass
class PermitRecord:
    size: int
    size_label: str
    permit_id: str
    permit_number: str
    permit_type: str
    permit_status: str
    status_group: str
    creation_date: str
    issued_date: str
    completed_date: str
    address: str
    specific_location: str
    owner_or_contact_info: str
    applicant: str | None
    applicant_address: str | None
    building_contractor: str | None
    building_contractor_address: str | None
    description: str
    permitElapsedDays: int | None
    projectValue: float | None
    PermitCategory: str | None
    PropertyUse: list[str] | None
    typeOfWork: str
    specificUseCategory: list[str] | None
    geom: dict[str, Any] | None
    geoLocalArea: str | None
    geo_point_2d: dict[str, float] | None
    source: str
    open_data_matched: bool
    has_geometry: bool
    geometry_source: str | None
    geocoded_address: str | None
    geocode_score: float | None
    geocode_source: str | None
    open_data_source: str | None
    detail_url: str
    matched_pattern: str
    match_context: str


class VisibleTextParser(HTMLParser):
    """Collects visible text while ignoring script/style content."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag in {"br", "p", "div", "tr", "li", "td", "th", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "tr", "li", "td", "th"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            text = data.strip()
            if text:
                self.parts.append(text)

    def text(self) -> str:
        merged = "".join(self.parts)
        merged = re.sub(r"[ \t\r\f\v]+", " ", merged)
        merged = re.sub(r" *\n *", "\n", merged)
        merged = re.sub(r"\n{2,}", "\n", merged)
        return html.unescape(merged).strip()


class HiddenInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        attr_map = dict(attrs)
        name = attr_map.get("name")
        if name:
            self.inputs[name] = attr_map.get("value", "")


class TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)

    def title(self) -> str:
        return clean_text("".join(self.parts))


class SpanTextParser(HTMLParser):
    """Extract text from individual spans on POSSE detail pages."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._open_spans: list[dict[str, Any]] = []
        self.spans: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return

        if tag == "span":
            attr_map = dict(attrs)
            self._open_spans.append(
                {
                    "id": attr_map.get("id", ""),
                    "name": attr_map.get("name", ""),
                    "class": attr_map.get("class", ""),
                    "title": attr_map.get("title", ""),
                    "parts": [],
                }
            )
        elif tag == "br" and not self._skip_depth:
            for span in self._open_spans:
                span["parts"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return

        if tag == "span" and self._open_spans:
            span = self._open_spans.pop()
            text = clean_text("".join(span["parts"]))
            if text:
                self.spans.append(
                    {
                        "id": span["id"],
                        "name": span["name"],
                        "class": span["class"],
                        "title": span["title"],
                        "text": text,
                    }
                )

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        for span in self._open_spans:
            span["parts"].append(data)


class TableLinkParser(HTMLParser):
    """Extracts table rows with links in a generic way."""

    def __init__(self) -> None:
        super().__init__()
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_table: list[list[dict[str, str]]] = []
        self.current_row: list[dict[str, str]] = []
        self.current_cell_parts: list[str] = []
        self.current_cell_link = ""
        self.tables: list[list[list[dict[str, str]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag == "table":
            self.in_table = True
            self.current_table = []
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.current_row = []
        elif tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self.current_cell_parts = []
            self.current_cell_link = ""
        elif tag == "a" and self.in_cell:
            href = attr_map.get("href", "")
            if href and not self.current_cell_link:
                self.current_cell_link = urllib.parse.urljoin(BASE_URL, href)
        elif tag == "br" and self.in_cell:
            self.current_cell_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.in_cell:
            cell_text = html.unescape("".join(self.current_cell_parts)).strip()
            cell_text = re.sub(r"\s+", " ", cell_text)
            self.current_row.append({"text": cell_text, "link": self.current_cell_link})
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.current_table.append(self.current_row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            if self.current_table:
                self.tables.append(self.current_table)
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell_parts.append(data)


class VancouverPermitClient:
    def __init__(self, timeout: int = 45) -> None:
        self.timeout = timeout
        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        self.geocode_cache: dict[str, dict[str, Any] | None] = {}

    def fetch_text(self, url: str, data: bytes | None = None) -> str:
        request = urllib.request.Request(url, data=data)
        with self.opener.open(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", "ignore")

    def fetch_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url)
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))

    def load_search_form(self) -> dict[str, str]:
        html_text = self.fetch_text(SEARCH_URL)
        parser = HiddenInputParser()
        parser.feed(html_text)
        required = ["currentpaneid", "paneid", "functiondef", "sortcolumns", "datachanges", "comesfrom"]
        missing = [name for name in required if name not in parser.inputs]
        if missing:
            raise RuntimeError(f"Search page is missing hidden fields: {', '.join(missing)}")
        return parser.inputs

    def search_by_created_date(
        self,
        created_from: dt.date,
        created_to: dt.date,
        permit_type: str | None,
    ) -> str:
        form = self.load_search_form()
        datachanges = [form["datachanges"]]
        datachanges.append(self._date_change(CREATED_DATE_FROM_COL, created_from))
        datachanges.append(self._date_change(CREATED_DATE_TO_COL, created_to))
        if permit_type:
            safe_type = permit_type.replace("\\", "\\\\").replace("'", "\\'")
            datachanges.append(f"('C','S0',{PERMIT_TYPE_COL},'{safe_type}')")
        datachanges.append("('F','ext-gen1009',0,0)")

        payload = {
            "currentpaneid": form["currentpaneid"],
            "paneid": form["paneid"],
            "functiondef": "5",
            "sortcolumns": form["sortcolumns"],
            "datachanges": ",".join(datachanges),
            "comesfrom": form["comesfrom"],
        }
        body = urllib.parse.urlencode(payload).encode("utf-8")
        response = self.fetch_text(SEARCH_URL, data=body)
        if "City of Vancouver - Error" in response:
            raise RuntimeError(
                "The permit portal returned an error page. The site likely changed its "
                "search payload or rejected the request."
            )
        return response

    def fetch_detail_text(self, detail_url: str) -> str:
        return self.fetch_text(detail_url)

    def lookup_issued_building_permit(self, permit_number: str) -> dict[str, Any] | None:
        if not permit_number:
            return None

        params = urllib.parse.urlencode(
            {
                "where": f'permitnumber="{permit_number}"',
                "limit": "1",
            }
        )
        data = self.fetch_json(f"{ISSUED_BUILDING_PERMITS_API}?{params}")
        results = data.get("results") or []
        if not results:
            return None
        return results[0]

    def geocode_address(self, address: str) -> dict[str, Any] | None:
        normalized = normalize_geocode_address(address)
        if not normalized:
            return None
        if normalized in self.geocode_cache:
            return self.geocode_cache[normalized]

        params = urllib.parse.urlencode(
            {
                "addressString": normalized,
                "localityName": "Vancouver",
                "maxResults": "1",
                "minScore": "70",
            }
        )
        try:
            data = self.fetch_json(f"{BC_GEOCODER_API}?{params}")
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            self.geocode_cache[normalized] = None
            return None

        features = data.get("features") or []
        if not features:
            self.geocode_cache[normalized] = None
            return None

        feature = features[0]
        self.geocode_cache[normalized] = feature
        return feature

    @staticmethod
    def _date_change(column_id: int, value: dt.date) -> str:
        return f"('C','S0',{column_id},'{value.isoformat()} 00:00:00')"


def parse_args() -> argparse.Namespace:
    today = dt.date.today()
    default_from = today - dt.timedelta(days=30)
    parser = argparse.ArgumentParser(
        description="Find Vancouver permit submissions that match a duplex to 8-plex size."
    )
    parser.add_argument(
        "size",
        help=(
            "Multiplex size selector: 2 through 8, a chain like 2-3-4, "
            "or 'all' for 2 through 8."
        ),
    )
    parser.add_argument(
        "--from-date",
        default=default_from.isoformat(),
        help="Created date start in YYYY-MM-DD format. Default: 30 days ago.",
    )
    parser.add_argument(
        "--to-date",
        default=today.isoformat(),
        help="Created date end in YYYY-MM-DD format. Default: today.",
    )
    parser.add_argument(
        "--permit-type",
        choices=["building", "development", "both"],
        default="both",
        help="Search only building permits, only development permits, or both.",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "csv"],
        default="json",
        help="Output format.",
    )
    parser.add_argument(
        "--output",
        help=(
            "Optional output path. Defaults to "
            "<plexType>_<currentTimeStamp>.json for JSON output. "
            "For multiple sizes, use a directory or omit this option."
        ),
    )
    return parser.parse_args()


def parse_size_selector(value: str) -> list[int]:
    normalized = value.strip().lower()
    if normalized == "all":
        return list(range(2, 9))

    parts = normalized.split("-")
    if not parts or any(not part.isdigit() for part in parts):
        raise SystemExit("Size must be 2 through 8, a chain like 2-3-4, or 'all'.")

    sizes: list[int] = []
    for part in parts:
        size = int(part)
        if size < 2 or size > 8:
            raise SystemExit("Every size must be between 2 and 8.")
        if size not in sizes:
            sizes.append(size)
    return sizes


def parse_iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    last_day = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][month - 1]
    day = min(value.day, last_day)
    return dt.date(year, month, day)


def iter_search_windows(start: dt.date, end: dt.date) -> Iterable[tuple[dt.date, dt.date]]:
    current = start
    while current <= end:
        next_start = add_months(current, 6)
        window_end = min(end, next_start - dt.timedelta(days=1))
        yield current, window_end
        current = window_end + dt.timedelta(days=1)


def permit_types_for_mode(mode: str) -> list[str | None]:
    if mode == "building":
        return ["Building Permit"]
    if mode == "development":
        return ["Development Permit"]
    return ["Building Permit", "Development Permit"]


def extract_detail_links(search_html: str) -> list[dict[str, str]]:
    parser = TableLinkParser()
    parser.feed(search_html)
    candidates: list[dict[str, str]] = []

    for table in parser.tables:
        for row in table:
            links = [cell["link"] for cell in row if cell["link"]]
            texts = [cell["text"] for cell in row if cell["text"]]
            if not links or not texts:
                continue
            row_blob = " | ".join(texts)
            if not re.search(r"\b(?:BP|DP|DB|DE|EL|GA|ME|PL|SU|TR)-\d{4}-\d+", row_blob, re.I):
                continue
            permit_number = first_match(r"\b[A-Z]{1,4}-\d{4}-\d+\b", row_blob)
            candidates.append(
                {
                    "permit_number": permit_number,
                    "row_text": row_blob,
                    "detail_url": links[0],
                }
            )

    deduped: dict[str, dict[str, str]] = {}
    for item in candidates:
        key = item["detail_url"]
        deduped[key] = item
    return list(deduped.values())


def visible_text_from_html(html_text: str) -> str:
    parser = VisibleTextParser()
    parser.feed(html_text)
    return parser.text()


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip(" \n\t:-")


def classify_size(text: str, size: int) -> tuple[bool, str, str]:
    lowered = text.lower()
    for pattern in SIZE_PATTERNS[size]:
        match = re.search(pattern, lowered, re.I)
        if match:
            context = context_for_match(text, match.start(), match.end())
            if is_negative_size_context(context, size):
                continue
            return True, match.group(0), context

    # Vancouver permit descriptions commonly say "Multiplex" or
    # "Multiple Dwelling" and then give the number of dwelling/residential
    # units. A bare phrase such as "building 4 unit #2871" is not enough.
    multiplex_markers = [
        r"\bmultiplex\b",
        r"\bmultiple dwelling\b",
        r"\bmultiple[- ]dwelling\b",
        r"\bmultiple conversion dwelling\b",
        r"\bmultiple[- ]conversion[- ]dwelling\b",
        r"\bmultiple residential\b",
        r"\bmultiple[- ]residential\b",
    ]
    unit_number = rf"(?:{size}|{NUMBER_WORDS[size]})"
    unit_count_markers = [
        rf"\btotal of {unit_number} (?:dwelling |residential |strata |rental |secured market rental )?units?\b",
        rf"\b(?:containing|contains|to contain|with|consisting of|for) (?:a )?(?:total of )?"
        rf"{unit_number} (?:dwelling |residential |strata |rental |secured market rental )?units?\b",
        rf"\b{unit_number}[- ](?:dwelling|residential|strata|rental)[- ]units?\b",
        rf"\b{unit_number}[- ]units?\b(?!\s*#)",
        rf"\b{unit_number} (?:dwelling|residential|strata|rental|secured market rental) units?\b",
    ]
    for pattern in unit_count_markers:
        for match in re.finditer(pattern, lowered, re.I):
            context = context_for_match(text, match.start(), match.end(), radius=350)
            if any(re.search(marker, context, re.I) for marker in multiplex_markers):
                return True, f"multiplex/multiple dwelling + {match.group(0)}", context

    return False, "", ""


def context_for_match(text: str, start: int, end: int, radius: int = 220) -> str:
    context_start = max(0, start - radius)
    context_end = min(len(text), end + radius)
    return clean_text(text[context_start:context_end])


def is_negative_size_context(context: str, size: int) -> bool:
    lowered = context.lower()
    terms = [SIZE_LABELS[size], rf"{size}[\s-]?plex", rf"{NUMBER_WORDS[size]}[\s-]?plex"]
    if size == 2:
        terms.append("two-family dwelling")
    term_group = "(?:" + "|".join(terms) + ")"
    one_family = r"(?:one[- ]family dwelling|single[- ]detached house|single detached house)"
    negative_patterns = [
        rf"\bchange\b.*\bexisting\b.*\b{term_group}\b.*\bto\b.*\b{one_family}\b",
        rf"\bconvert\b.*\b{term_group}\b.*\bto\b.*\b{one_family}\b",
        rf"\bdemolish(?:e?s|ed|ing)?\b.*\bexisting\b.*\b{term_group}\b",
        rf"\bexisting\b.*\b{term_group}\b.*\bdemolish(?:e?s|ed|ing)?\b",
        rf"\b{term_group}\b\s+to\s+(?:a\s+)?{one_family}\b",
    ]
    return any(re.search(pattern, lowered, re.I | re.S) for pattern in negative_patterns)


def extract_first_field(text: str, labels: Iterable[str]) -> str:
    for label in labels:
        match = re.search(
            rf"(?:^|\n){re.escape(label)}\s*:?\s*\n(.+?)(?=\n[A-Z][A-Za-z0-9 /&()#.-]{{2,40}}\s*:?\s*\n|\Z)",
            text,
            re.I | re.S | re.M,
        )
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" -:\n")
            if value:
                return value
    return ""


def parse_detail_page(detail_html: str) -> dict[str, Any]:
    title_parser = TitleParser()
    title_parser.feed(detail_html)
    title = title_parser.title()

    span_parser = SpanTextParser()
    span_parser.feed(detail_html)
    spans = span_parser.spans

    permit_number = first_match(r"\b[A-Z]{1,4}-\d{4}-\d+\b", title)
    status = ""
    status_match = re.search(
        r'<div[^>]+class=["\']permitStatusDisplay["\'][^>]*>(.*?)</div>',
        detail_html,
        re.I | re.S,
    )
    if status_match:
        status = clean_text(re.sub(r"<[^>]+>", " ", status_match.group(1)))

    return {
        "title": title,
        "permit_number": permit_number,
        "permit_type": infer_permit_type(title, permit_number),
        "status": status,
        "application_date": span_text_by_id_prefix(spans, ["ApplicationDate_", "CreatedDate_"]),
        "issue_date": span_text_by_id_prefix(spans, ["IssueDate_"]),
        "completed_date": span_text_by_id_prefix(spans, ["CompletedDate_"]),
        "primary_location": span_text_by_id_prefix(spans, ["PermitLocation_"]),
        "specific_location": span_text_by_id_prefix(spans, ["JobLocation_"]),
        "work_description": span_text_by_id_prefix(spans, ["WorkDescription_"]),
        "type_of_work": span_text_by_id_prefix(spans, ["TypeOfWork_"]),
        "owner": span_text_by_id_prefix(spans, ["Owner_", "OwnerName_", "PropertyOwner_"]),
        "owner_contact": span_text_by_id_prefix(spans, ["OwnerContact_", "Contact_"]),
    }


def span_text_by_id_prefix(spans: list[dict[str, str]], prefixes: Iterable[str]) -> str:
    for prefix in prefixes:
        for span in spans:
            span_id = span.get("id", "")
            if span_id.startswith(prefix):
                text = span.get("text", "")
                if text:
                    return text
    return ""


def infer_permit_type(title: str, permit_number: str) -> str:
    if "Development Permit" in title:
        return "Development Permit"
    if "Building Permit" in title:
        return "Building Permit"
    if permit_number.startswith(("DP-", "DE-")):
        return "Development Permit"
    if permit_number.startswith(("BP-", "DB-")):
        return "Building Permit"
    return ""


def normalize_status(status: str) -> str:
    lowered = status.lower()
    if any(token in lowered for token in ["completed", "closed", "final"]):
        return "completed"
    if any(token in lowered for token in ["issued", "approved"]):
        return "approved"
    if any(token in lowered for token in ["submitted", "in review", "review", "ready"]):
        return "pending"
    if any(token in lowered for token in ["cancelled", "canceled", "withdrawn", "expired"]):
        return "cancelled"
    return "unknown" if not status else "other"


def combine_contact(*values: str | None) -> str:
    return "; ".join(value for value in values if value)


def open_data_value(open_data: dict[str, Any] | None, key: str) -> Any:
    if not open_data:
        return None
    return open_data.get(key)


def normalize_geocode_address(address: str) -> str:
    value = clean_text(address)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*=\s*Primary Address\b", "", value, flags=re.I)
    value = re.sub(r"\s*,?\s*Canada\b", "", value, flags=re.I)
    return value.strip(" ,")


def record_has_geometry(geom: dict[str, Any] | None, geo_point_2d: dict[str, float] | None) -> bool:
    if geo_point_2d and geo_point_2d.get("lat") is not None and geo_point_2d.get("lon") is not None:
        return True

    coordinates = (geom or {}).get("geometry", {}).get("coordinates")
    return isinstance(coordinates, list) and len(coordinates) >= 2


def geometry_from_geocode(geocode: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, float] | None]:
    if not geocode:
        return None, None

    coordinates = geocode.get("geometry", {}).get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None, None

    lon, lat = coordinates[0], coordinates[1]
    if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return None, None

    return (
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
            "properties": {},
        },
        {"lon": lon, "lat": lat},
    )


def build_record(
    size: int,
    detail_url: str,
    detail_html: str,
    search_row_text: str,
    permit_number_hint: str,
    matched_pattern: str,
    match_context: str,
    open_data: dict[str, Any] | None = None,
    geocode: dict[str, Any] | None = None,
) -> PermitRecord:
    visible_text = visible_text_from_html(detail_html)
    detail_fields = parse_detail_page(detail_html)
    permit_number = (
        detail_fields["permit_number"]
        or extract_first_field(visible_text, ["Permit Number", "Application Number"])
        or permit_number_hint
        or first_match(r"\b[A-Z]{1,4}-\d{4}-\d+\b", visible_text)
    )
    permit_type = (
        detail_fields["permit_type"]
        or extract_first_field(visible_text, ["Type", "Permit Type", "Application Type"])
    )
    status = detail_fields["status"] or status_from_search_row(search_row_text)
    created_date = (
        detail_fields["application_date"]
        or open_data_value(open_data, "permitnumbercreateddate")
        or extract_first_field(visible_text, ["Created Date", "Application Date"])
    )
    issue_date = detail_fields["issue_date"] or open_data_value(open_data, "issuedate") or ""
    completed_date = detail_fields["completed_date"]
    address = (
        detail_fields["primary_location"]
        or open_data_value(open_data, "address")
        or extract_first_field(visible_text, ["Primary Location", "Address", "Location", "Project Address"])
    )
    description = (
        detail_fields["work_description"]
        or open_data_value(open_data, "projectdescription")
        or extract_first_field(
            visible_text,
            ["Work Description", "Description", "Project Description", "Permit Description"],
        )
    )
    owner_or_contact_info = combine_contact(
        detail_fields["owner"],
        detail_fields["owner_contact"],
        open_data_value(open_data, "applicant"),
        open_data_value(open_data, "buildingcontractor"),
    )

    if not description:
        description = first_sentence_with_keyword(visible_text, matched_pattern)
    if not address:
        address = first_match(r"\b\d{1,5}\s+[A-Za-z0-9 .'-]+(?:Vancouver|BC)\b", visible_text)

    geom = open_data_value(open_data, "geom")
    geo_point_2d = open_data_value(open_data, "geo_point_2d")
    geometry_source = "issued_building_permits" if record_has_geometry(geom, geo_point_2d) else None
    geocoded_address = None
    geocode_score = None
    geocode_source = None

    if not geometry_source and geocode:
        geocode_geom, geocode_point = geometry_from_geocode(geocode)
        if geocode_geom and geocode_point:
            geom = geocode_geom
            geo_point_2d = geocode_point
            geometry_source = "bc_geocoder"
            geocoded_address = geocode.get("properties", {}).get("fullAddress")
            geocode_score = geocode.get("properties", {}).get("score")
            geocode_source = BC_GEOCODER_API

    has_geometry = record_has_geometry(geom, geo_point_2d)

    return PermitRecord(
        size=size,
        size_label=SIZE_LABELS[size],
        permit_id=permit_number,
        permit_number=permit_number,
        permit_type=permit_type,
        permit_status=status,
        status_group=normalize_status(status),
        creation_date=created_date,
        issued_date=issue_date,
        completed_date=completed_date,
        address=address,
        specific_location=detail_fields["specific_location"],
        owner_or_contact_info=owner_or_contact_info,
        applicant=open_data_value(open_data, "applicant"),
        applicant_address=open_data_value(open_data, "applicantaddress"),
        building_contractor=open_data_value(open_data, "buildingcontractor"),
        building_contractor_address=open_data_value(open_data, "buildingcontractoraddress"),
        description=description,
        permitElapsedDays=open_data_value(open_data, "permitelapseddays"),
        projectValue=open_data_value(open_data, "projectvalue"),
        PermitCategory=open_data_value(open_data, "permitcategory"),
        PropertyUse=open_data_value(open_data, "propertyuse"),
        typeOfWork=detail_fields["type_of_work"] or open_data_value(open_data, "typeofwork") or "",
        specificUseCategory=open_data_value(open_data, "specificusecategory"),
        geom=geom,
        geoLocalArea=open_data_value(open_data, "geolocalarea"),
        geo_point_2d=geo_point_2d,
        source="permit_portal",
        open_data_matched=open_data is not None,
        has_geometry=has_geometry,
        geometry_source=geometry_source if has_geometry else None,
        geocoded_address=geocoded_address,
        geocode_score=geocode_score,
        geocode_source=geocode_source,
        open_data_source=ISSUED_BUILDING_PERMITS_API if open_data else None,
        detail_url=detail_url,
        matched_pattern=matched_pattern,
        match_context=match_context,
    )


def first_match(pattern: str, text: str) -> str:
    match = re.search(pattern, text, re.I)
    return match.group(0) if match else ""


def status_from_search_row(row_text: str) -> str:
    if not row_text:
        return ""
    parts = [part.strip() for part in row_text.split("|")]
    for part in parts:
        if part.lower() in {
            "submitted",
            "in review",
            "issued",
            "completed",
            "cancelled",
            "canceled",
            "withdrawn",
            "expired",
            "ready for issuance",
        }:
            return part
    return ""


def first_sentence_with_keyword(text: str, keyword: str) -> str:
    if not keyword:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        if keyword.lower() in sentence.lower():
            return sentence.strip()
    return ""


def print_table(records: list[PermitRecord]) -> None:
    if not records:
        print("No matching permits found.")
        return

    headers = ["Permit", "Type", "Created", "Status", "Address", "Match"]
    rows = [
        [
            record.permit_id or "-",
            record.permit_type or "-",
            record.creation_date or "-",
            shorten(record.permit_status, 38) or "-",
            shorten(record.address, 42) or "-",
            record.matched_pattern,
        ]
        for record in records
    ]
    widths = [
        max(len(headers[i]), max(len(row[i]) for row in rows))
        for i in range(len(headers))
    ]
    header_line = "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    rule_line = "  ".join("-" * widths[i] for i in range(len(headers)))
    print(header_line)
    print(rule_line)
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def default_output_path(size: int, output_format: str) -> str:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    extension = "csv" if output_format == "csv" else "json"
    return f"{SIZE_LABELS[size]}_{timestamp}.{extension}"


def output_path_for_size(
    requested_output: str | None,
    size: int,
    output_format: str,
    multiple_sizes: bool,
) -> str | None:
    if output_format == "table":
        return None

    if not requested_output:
        return default_output_path(size, output_format)

    output = Path(requested_output)
    if multiple_sizes:
        if output.suffix:
            suffix = output.suffix
            parent = output.parent if str(output.parent) != "." else Path(".")
            return str(parent / f"{output.stem}_{SIZE_LABELS[size]}{suffix}")
        output.mkdir(parents=True, exist_ok=True)
        return str(output / default_output_path(size, output_format))

    return str(output)


def write_output(records: list[PermitRecord], output_format: str, output_path: str) -> None:
    if output_format == "json":
        with open(output_path, "w", encoding="utf-8", newline="") as handle:
            json.dump([asdict(record) for record in records], handle, indent=2)
        return

    if output_format == "csv":
        with open(output_path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(records[0]).keys()) if records else list(PermitRecord.__dataclass_fields__.keys()))
            writer.writeheader()
            for record in records:
                writer.writerow(asdict(record))
        return

    raise ValueError(f"Unsupported output format: {output_format}")


def scrape_size(
    client: VancouverPermitClient,
    size: int,
    created_from: dt.date,
    created_to: dt.date,
    permit_type_mode: str,
) -> list[PermitRecord]:
    records: list[PermitRecord] = []
    seen_numbers: set[str] = set()

    for permit_type in permit_types_for_mode(permit_type_mode):
        for window_start, window_end in iter_search_windows(created_from, created_to):
            search_html = client.search_by_created_date(window_start, window_end, permit_type)
            detail_rows = extract_detail_links(search_html)
            for row in detail_rows:
                detail_html = client.fetch_detail_text(row["detail_url"])
                detail_text = visible_text_from_html(detail_html)
                matched, matched_pattern, match_context = classify_size(detail_text, size)
                if not matched:
                    continue
                permit_number_hint = row["permit_number"]
                detail_fields = parse_detail_page(detail_html)
                permit_number = detail_fields["permit_number"] or permit_number_hint
                open_data = client.lookup_issued_building_permit(permit_number)
                geocode = None
                if not record_has_geometry(
                    open_data_value(open_data, "geom"),
                    open_data_value(open_data, "geo_point_2d"),
                ):
                    address_hint = detail_fields["primary_location"] or first_match(
                        r"\b\d{1,5}\s+[A-Za-z0-9 .'-]+(?:Vancouver|BC)\b",
                        detail_text,
                    )
                    geocode = client.geocode_address(address_hint)
                record = build_record(
                    size=size,
                    detail_url=row["detail_url"],
                    detail_html=detail_html,
                    search_row_text=row["row_text"],
                    permit_number_hint=permit_number_hint,
                    matched_pattern=matched_pattern,
                    match_context=match_context,
                    open_data=open_data,
                    geocode=geocode,
                )
                dedupe_key = record.permit_id or row["detail_url"]
                if dedupe_key in seen_numbers:
                    continue
                seen_numbers.add(dedupe_key)
                records.append(record)

    records.sort(key=lambda record: (record.creation_date, record.permit_id))
    return records


def emit_records(
    records: list[PermitRecord],
    size: int,
    output_format: str,
    output_path: str | None,
) -> None:
    if output_path:
        write_output(records, output_format, output_path)
        print(f"Wrote {len(records)} matching permits to {output_path}")
    elif output_format == "json":
        print(json.dumps([asdict(record) for record in records], indent=2))
    elif output_format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=list(asdict(records[0]).keys()) if records else list(PermitRecord.__dataclass_fields__.keys()))
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))
    else:
        print_table(records)
        if records:
            print()
            print(f"Found {len(records)} {SIZE_LABELS[size]} matches.")


def main() -> int:
    args = parse_args()
    sizes = parse_size_selector(args.size)
    created_from = parse_iso_date(args.from_date)
    created_to = parse_iso_date(args.to_date)
    if created_from > created_to:
        raise SystemExit("--from-date must be on or before --to-date.")

    client = VancouverPermitClient()
    multiple_sizes = len(sizes) > 1

    try:
        for size in sizes:
            if multiple_sizes:
                print(f"Searching {SIZE_LABELS[size]}...")
            records = scrape_size(
                client=client,
                size=size,
                created_from=created_from,
                created_to=created_to,
                permit_type_mode=args.permit_type,
            )
            output_path = output_path_for_size(
                requested_output=args.output,
                size=size,
                output_format=args.format,
                multiple_sizes=multiple_sizes,
            )
            emit_records(records, size, args.format, output_path)
    except urllib.error.HTTPError as exc:
        print(f"HTTP error while talking to the Vancouver permit portal: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error while talking to the Vancouver permit portal: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
