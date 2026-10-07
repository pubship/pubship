"""Read original Play CSV metrics with explicit date coverage and pagination."""

import csv
import gzip
import io
import re
from datetime import date, timedelta
from urllib.parse import quote, urlencode

from . import http
from .auth import Auth
from .config import Settings
from .errors import PlayError

DIMENSIONS = {
    "installs": {
        "overview",
        "country",
        "app_version",
        "device",
        "language",
        "os_version",
        "carrier",
    },
    "ratings": {
        "overview",
        "country",
        "app_version",
        "device",
        "language",
        "os_version",
        "carrier",
    },
    "store_performance": {"country", "traffic_source"},
}


def prefix(package, family, month):
    if family not in DIMENSIONS or not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", month):
        raise PlayError("Use installs, ratings or store_performance and a YYYYMM month.")
    return f"stats/{family}/{family}_{package}_{month}_"


def parse_csv(raw, package):
    try:
        if raw.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(http.MAX_BYTES + 1)
        if len(raw) > http.MAX_BYTES:
            raise PlayError("Expanded report exceeds 20 MiB.")
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        reader = csv.DictReader(io.StringIO(raw.decode(encoding)))
        columns = reader.fieldnames or []
        package_column = next((c for c in columns if c.lower() == "package name"), None)
        if "Date" not in columns or not package_column or len(columns) != len(set(columns)):
            raise PlayError("Unexpected Play report columns.")
        rows = []
        for row in reader:
            if None in row or None in row.values() or row[package_column] != package:
                raise PlayError("Malformed report or unexpected app in CSV.")
            date.fromisoformat(row["Date"])
            rows.append(row)
            if len(rows) > 100000:
                raise PlayError("Report exceeds 100,000 rows.")
        return columns, rows
    except (UnicodeError, ValueError, OSError, EOFError, csv.Error):
        raise PlayError("Report is not valid UTF-8/UTF-16 CSV.") from None


class Reports:
    def __init__(self, settings: Settings, auth: Auth):
        self.settings, self.auth = settings, auth

    def files(self, package, family, month, page_token=""):
        self.settings.require_package(package)
        self.settings.require_bucket()
        if len(page_token) > 4096:
            raise PlayError("Pagination token is too long.")
        params = {
            "prefix": prefix(package, family, month),
            "maxResults": 100,
            "fields": "items(name,size,updated,generation),nextPageToken",
        }
        if page_token:
            params["pageToken"] = page_token
        data = http.get(
            f"https://storage.googleapis.com/storage/v1/b/{self.settings.bucket}/o?"
            + urlencode(params),
            self.auth.token("storage"),
        )
        return {
            "package": package,
            "family": family,
            "month": month,
            "data": data,
            "note": "Follow nextPageToken if present. Object update time is not the last date in the CSV. Absent reports are unavailable, not zero activity.",
        }

    def read(self, package, family, month, dimension, start_date, end_date, offset=0, limit=100):
        self.settings.require_package(package)
        self.settings.require_bucket()
        base_prefix = prefix(package, family, month)
        try:
            first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except (TypeError, ValueError):
            raise PlayError("Use ISO dates YYYY-MM-DD.") from None
        if first > last or first.strftime("%Y%m") != month or last.strftime("%Y%m") != month:
            raise PlayError("Use an ordered date range within one report month.")
        if (
            dimension not in DIMENSIONS[family]
            or not 1 <= limit <= 1000
            or not 0 <= offset <= 100000
        ):
            raise PlayError("Unsupported report dimension or row pagination; limit is 1–1000.")
        name = base_prefix + dimension + ".csv"
        base = f"https://storage.googleapis.com/storage/v1/b/{self.settings.bucket}/o/" + quote(
            name, safe=""
        )
        token = self.auth.token("storage")
        metadata = http.get(base + "?fields=name,updated,generation", token)
        if not isinstance(metadata, dict) or not re.fullmatch(
            r"\d+", str(metadata.get("generation", ""))
        ):
            raise PlayError("Report metadata omitted a valid object generation.")
        params = urlencode({"alt": "media", "generation": metadata["generation"]})
        columns, all_rows = parse_csv(http.get(base + "?" + params, token, raw=True), package)
        rows = [r for r in all_rows if first <= date.fromisoformat(r["Date"]) <= last]
        present = {r["Date"] for r in rows}
        missing = [
            (first + timedelta(days=n)).isoformat()
            for n in range((last - first).days + 1)
            if (first + timedelta(days=n)).isoformat() not in present
        ]
        covered = sorted({r["Date"] for r in all_rows})
        return {
            "source": "gs://" + self.settings.bucket + "/" + name,
            "metadata": metadata,
            "requested_range": [start_date, end_date],
            "report_first_date": covered[0] if covered else None,
            "report_last_date": covered[-1] if covered else None,
            "dates_without_rows": missing,
            "columns": columns,
            "matching_rows": len(rows),
            "rows": rows[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "note": "Missing rows are not zero. Counts may be suppressed/grouped as Other. Preserve metric names; do not sum daily stocks or average daily conversion rates. Country and traffic-source reports describe the same activity; do not add their totals.",
        }
