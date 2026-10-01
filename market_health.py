"""Official exchange calendars and completed-session health contracts."""

import csv
import datetime as dt
import hashlib
import html
import html.parser
import io
import json
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
NEW_YORK = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
SCHEMA_VERSION = 2
CALENDAR_TTL = dt.timedelta(hours=24)
TWSE_URL = "https://www.twse.com.tw/holidaySchedule/holidaySchedule?response=html"
CBOE_URL = "https://www.cboe.com/us/options/holidays/csv/"
TAIPEI_EOC_URL = "https://eoc.gov.taipei/News?MenuId=51"
TAIPEI_CLOSURE_HISTORY_URL = "https://dop.gov.taipei/cp.aspx?n=EFE42F770DFD63FB"
KNOWN_VERIFIED_TAIWAN_EXCEPTIONAL_CLOSURES = (
    {
        "date": "2026-07-10",
        "state": "closed",
        "source": "TAIPEI_EOC_OFFICIAL_ANNOUNCEMENT",
        "evidenceId": "https://eoc.gov.taipei/News/Detail/909",
        "updatedAt": "2026-07-09T20:00:00+08:00",
    },
)


class CalendarUnavailable(ValueError):
    """Raised when no fresh official calendar can safely validate sessions."""


class _TableRows(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _request_text(url, deadline=None):
    last_error = None
    for attempt in range(3):
        try:
            remaining = deadline - time.monotonic() if deadline is not None else 12
            if remaining <= 0:
                raise TimeoutError("market acquisition deadline exceeded")
            request = Request(url, headers={"User-Agent": "SkynetMarketHealth/1.0"})
            request_options = {"timeout": min(12, remaining)}
            if url in (TWSE_URL, TAIPEI_EOC_URL, TAIPEI_CLOSURE_HISTORY_URL):
                context = ssl.create_default_context()
                strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
                if strict_flag:
                    # TWSE's trusted certificate chain omits an optional issuer
                    # Subject Key Identifier. Keep CA and hostname validation,
                    # while avoiding OpenSSL's extra strict-profile rejection.
                    context.verify_flags &= ~strict_flag
                request_options["context"] = context
            with urlopen(request, **request_options) as response:
                return response.read().decode("utf-8-sig")
        except HTTPError as error:
            last_error = error
            if error.code not in (429, 500, 502, 503, 504):
                break
        except (TimeoutError, URLError, OSError) as error:
            last_error = error
        if attempt < 2:
            delay = 0.25 * (2 ** attempt)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            time.sleep(delay)
    raise RuntimeError(f"official calendar request failed ({type(last_error).__name__})")


def parse_twse_calendar(body):
    parser = _TableRows()
    parser.feed(body)
    closed = set()
    years = set()
    for row in parser.rows:
        joined = " ".join(row)
        match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", joined)
        if not match:
            continue
        session_date = dt.date(*(int(part) for part in match.groups()))
        years.add(session_date.year)
        name = row[1] if len(row) > 1 else joined
        # The official annual table also lists the first/last trading day.
        # Only explicit no-trade/holiday rows close an otherwise weekday session.
        if "交易日" not in name or "無交易" in name or "休市" in name:
            closed.add(session_date.isoformat())
    if not years:
        raise ValueError("TWSE calendar format changed or contains no dated rows")
    return {"closedDates": sorted(closed), "years": sorted(years)}


def parse_cboe_calendar(body):
    # Cboe prefixes the CSV with metadata and a ## delimiter. The metadata
    # currently encodes its newlines as literal backslash-n sequences.
    normalized = body
    if body.startswith("# Generated:"):
        marker = body.rfind("##")
        if marker < 0:
            raise ValueError("Cboe calendar metadata delimiter is missing")
        normalized = body[marker + 2:].lstrip("\r\n")
    reader = csv.DictReader(io.StringIO(normalized))
    required = {"Holiday Name", "Date", "Regular Trading Hours"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError("Cboe holiday CSV schema changed")
    closed = set()
    years = set()
    rows = 0
    for row in reader:
        raw_date = (row.get("Date") or "").strip()
        if not raw_date:
            continue
        try:
            session_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            session_date = dt.datetime.strptime(raw_date, "%m/%d/%Y").date()
        years.add(session_date.year)
        rows += 1
        regular_hours = (row.get("Regular Trading Hours") or "").strip()
        if regular_hours.lower() in ("none", "closed"):
            closed.add(session_date.isoformat())
        elif not regular_hours:
            raise ValueError("Cboe holiday CSV has an ambiguous trading-hours row")
    if not years or rows == 0:
        raise ValueError("Cboe holiday CSV contains no dated rows")
    return {"closedDates": sorted(closed), "years": sorted(years)}


def _checked_at(now):
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parsed_calendar_hash(twse, cboe, special_closures):
    content = json.dumps(
        {"twse": twse, "cboe": cboe, "specialClosures": special_closures},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class _StructuredRows(html.parser.HTMLParser):
    """Keep official table cell boundaries and link targets while parsing HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.active_link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"text": [], "links": []}
        elif tag == "a" and self.cell is not None:
            self.active_link = {"href": attrs.get("href", ""), "text": []}
        elif tag in ("br", "p", "li") and self.cell is not None:
            self.cell["text"].append("\n")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"].append(data)
            if self.active_link is not None:
                self.active_link["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.active_link is not None:
            self.cell["links"].append({
                "href": self.active_link["href"],
                "text": " ".join(" ".join(self.active_link["text"]).split()),
            })
            self.active_link = None
        elif tag in ("td", "th") and self.row is not None and self.cell is not None:
            lines = [" ".join(line.split()) for line in "".join(self.cell["text"]).splitlines()]
            self.row.append({"text": "\n".join(line for line in lines if line), "links": self.cell["links"]})
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _closure_record(day, source, evidence_id, updated_at, state="closed"):
    return {
        "date": day.isoformat(),
        "state": state,
        "source": source,
        "evidenceId": evidence_id,
        "updatedAt": updated_at.isoformat(),
    }


def _roc_year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year + 1911 if 1 <= year <= 300 else (year if year >= 1911 else None)


def parse_taipei_closure_history(body, now=None):
    """Read the Taipei HR Department's dated official history of full-day closures."""
    now = now or dt.datetime.now(TAIPEI)
    parser = _StructuredRows()
    parser.feed(body)
    headers = [" ".join(cell["text"].split()) for row in parser.rows for cell in row]
    if not any(value == "年" for value in headers) or not any("天然災害名稱" in value for value in headers) or not any("停止上班上課情形" in value for value in headers):
        raise ValueError("Taipei HR closure history schema changed")
    records = []
    current_year = None
    current_event = ""
    for row in parser.rows:
        year = _roc_year(row[0]["text"].strip()) if row else None
        if year is not None and len(row) >= 3:
            current_year = year
            current_event = row[1]["text"].strip()
            description = row[2]["text"]
        elif current_year is not None:
            description = "\n".join(cell["text"] for cell in row if cell["text"])
        else:
            continue
        evidence = f"{current_year}:{current_event}"[:160]
        for match in re.finditer(r"(?:^|\n)\s*(\d{1,2})月\s*(\d{1,2})日\s*停止上班及上課[。.]?", description):
            try:
                day = dt.date(current_year, int(match.group(1)), int(match.group(2)))
            except ValueError:
                continue
            if day <= now.date() and day >= now.date() - dt.timedelta(days=450):
                records.append(_closure_record(day, "TAIPEI_HR_OFFICIAL_HISTORY", evidence, now))
    return records


def _date_from_eoc_title(title, published_at):
    full = re.search(r"(?<!\d)(20\d{2})[./-](\d{1,2})[./-](\d{1,2})(?!\d)", title)
    if full:
        try:
            return dt.date(*(int(part) for part in full.groups()))
        except ValueError:
            return None
    roc = re.search(r"(?<!\d)(1\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", title)
    if roc:
        try:
            return dt.date(int(roc.group(1)) + 1911, int(roc.group(2)), int(roc.group(3)))
        except ValueError:
            return None
    short = re.search(r"(?:明|今|今日|明日)?[（(]?\s*(\d{1,2})\s*/\s*(\d{1,2})\s*[）)]?", title)
    if short:
        month, day = int(short.group(1)), int(short.group(2))
        try:
            candidates = [dt.date(published_at.year + delta, month, day) for delta in (-1, 0, 1)]
        except ValueError:
            return None
        return min(candidates, key=lambda value: abs((value - published_at.date()).days))
    if any(token in title for token in ("明（明日）", "明日", "明（")):
        return published_at.date() + dt.timedelta(days=1)
    if any(token in title for token in ("今（今日）", "今天", "今日")):
        return published_at.date()
    return None


def parse_taipei_eoc_closures(body, now=None):
    """Read explicit citywide full-day closure announcements from Taipei EOC news."""
    now = now or dt.datetime.now(TAIPEI)
    parser = _StructuredRows()
    parser.feed(body)
    headers = [" ".join(cell["text"].split()) for row in parser.rows for cell in row]
    if not any(value in ("發布時間", "發佈時間") for value in headers) or not any(value in ("發布單位", "發佈單位") for value in headers) or not any(value == "標題" for value in headers):
        raise ValueError("Taipei EOC news schema changed")
    records = []
    for row in parser.rows:
        if len(row) < 4:
            continue
        published_text = row[1]["text"].strip()
        publisher = row[2]["text"].strip()
        title_cell = row[-1]
        title = title_cell["text"].strip()
        if not title or not ("臺北市" in title or "台北市" in title):
            continue
        is_closure = "停止上班及上課" in title
        is_cancel = "照常上班及上課" in title or "取消" in title
        if not (is_closure or is_cancel):
            continue
        if any(token in title for token in ("上午", "下午", "半日", "部分")):
            continue
        if "秘書處" not in publisher and "市政府" not in publisher:
            continue
        try:
            published_at = dt.datetime.strptime(published_text, "%Y/%m/%d %H:%M").replace(tzinfo=TAIPEI)
        except ValueError:
            continue
        target_day = _date_from_eoc_title(title, published_at)
        if target_day is None or target_day < now.date() - dt.timedelta(days=7) or target_day > now.date() + dt.timedelta(days=7):
            continue
        evidence_id = next((link["href"] for link in title_cell["links"] if link["href"]), title[:120])
        state = "cancelled" if is_cancel else "closed"
        records.append(_closure_record(target_day, "TAIPEI_EOC_OFFICIAL_ANNOUNCEMENT", evidence_id, published_at, state))
    return records


def _verified_cached_calendar(path):
    try:
        with open(path, encoding="utf-8") as file:
            cached = json.load(file)
        if cached.get("schemaVersion") != SCHEMA_VERSION:
            return None
        twse = cached.get("twse")
        cboe = cached.get("cboe")
        special = cached.get("specialClosures")
        if not isinstance(twse, dict) or not isinstance(cboe, dict) or not isinstance(special, list):
            return None
        if cached.get("contentHash") != _parsed_calendar_hash(twse, cboe, special):
            return None
        return cached
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _valid_cached_calendar(path, now):
    try:
        cached = _verified_cached_calendar(path)
        if cached is None:
            return None
        twse = cached.get("twse")
        cboe = cached.get("cboe")
        for source in (twse, cboe):
            if not isinstance(source.get("years"), list) or not isinstance(source.get("closedDates"), list):
                return None
            for raw_date in source["closedDates"]:
                day = dt.date.fromisoformat(raw_date)
                if day.year not in source["years"]:
                    return None
        checked = dt.datetime.fromisoformat(cached["checkedAt"].replace("Z", "+00:00"))
        age = now.astimezone(UTC) - checked.astimezone(UTC)
        if age < dt.timedelta(0) or age > CALENDAR_TTL:
            return None
        if now.year not in cached["twse"]["years"] or now.year not in cached["cboe"]["years"]:
            return None
        return cached
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def load_calendars(now=None, cache_dir=".calendar-cache", deadline=None):
    now = now or dt.datetime.now(TAIPEI)
    cache_path = os.path.join(cache_dir, "market-calendar.json")
    prior = _verified_cached_calendar(cache_path)
    try:
        twse_text = _request_text(TWSE_URL, deadline) if deadline is not None else _request_text(TWSE_URL)
        cboe_text = _request_text(CBOE_URL, deadline) if deadline is not None else _request_text(CBOE_URL)
        eoc_text = _request_text(TAIPEI_EOC_URL, deadline) if deadline is not None else _request_text(TAIPEI_EOC_URL)
        twse = parse_twse_calendar(twse_text)
        cboe = parse_cboe_calendar(cboe_text)
        if now.year not in twse["years"] or now.year not in cboe["years"]:
            raise ValueError("official calendars do not cover the current year")
        prior_records = prior.get("specialClosures", []) if prior else []
        by_date = {item["date"]: item for item in KNOWN_VERIFIED_TAIWAN_EXCEPTIONAL_CLOSURES}
        for item in prior_records:
            by_date[item["date"]] = item
        try:
            history_text = _request_text(TAIPEI_CLOSURE_HISTORY_URL, deadline) if deadline is not None else _request_text(TAIPEI_CLOSURE_HISTORY_URL)
        except RuntimeError:
            if prior is None:
                raise
            history_text = ""
        for item in parse_taipei_closure_history(history_text, now):
            by_date[item["date"]] = item
        for item in parse_taipei_eoc_closures(eoc_text, now):
            by_date[item["date"]] = item
        special_closures = sorted(by_date.values(), key=lambda item: (item["date"], item["updatedAt"]))
        active_special_dates = {
            item["date"] for item in special_closures if item["state"] == "closed"
        }
        twse["closedDates"] = sorted(set(twse["closedDates"]) | active_special_dates)
        evidence = (twse_text + "\n" + cboe_text + "\n" + eoc_text + "\n" + history_text).encode("utf-8")
        calendars = {
            "schemaVersion": SCHEMA_VERSION,
            "checkedAt": _checked_at(now),
            "sha256": hashlib.sha256(evidence).hexdigest(),
            "contentHash": _parsed_calendar_hash(twse, cboe, special_closures),
            "twse": twse,
            "cboe": cboe,
            "specialClosures": special_closures,
            "temporaryClosureSource": "Taipei City EOC announcements + HR Department official history",
        }
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as file:
            json.dump(calendars, file, ensure_ascii=False, sort_keys=True)
        calendars["cacheUsed"] = False
        return calendars
    except Exception as error:
        cached = _valid_cached_calendar(cache_path, now)
        if cached:
            cached["cacheUsed"] = True
            cached["cacheReason"] = type(error).__name__
            return cached
        raise CalendarUnavailable(f"official calendar unavailable: {type(error).__name__}") from error


def _is_open(day, calendar):
    if day.year not in calendar["years"]:
        raise CalendarUnavailable(f"calendar coverage missing for {day.year}")
    return day.weekday() < 5 and day.isoformat() not in set(calendar["closedDates"])


def _previous_open(day, calendar, *, include_day=True):
    candidate = day if include_day else day - dt.timedelta(days=1)
    for _ in range(370):
        if _is_open(candidate, calendar):
            return candidate
        candidate -= dt.timedelta(days=1)
    raise CalendarUnavailable("calendar has no previous session in its coverage")


def _next_open(day, calendar):
    candidate = day + dt.timedelta(days=1)
    for _ in range(370):
        if _is_open(candidate, calendar):
            return candidate
        candidate += dt.timedelta(days=1)
    raise CalendarUnavailable("calendar has no next session in its coverage")


def expected_taiwan_session(now, calendar):
    now = now.astimezone(TAIPEI)
    today = now.date()
    after_close_buffer = (now.hour, now.minute) >= (14, 30)
    if after_close_buffer and _is_open(today, calendar):
        return today
    return _previous_open(today, calendar, include_day=not _is_open(today, calendar))


def taiwan_next_due(now, expected, calendar):
    session = _next_open(expected, calendar)
    return dt.datetime.combine(session, dt.time(14, 30), TAIPEI)


def expected_us_session(now, calendar):
    now = now.astimezone(TAIPEI)
    candidate = now.date() - dt.timedelta(days=7)
    found = None
    # VIX's latest fully publishable session cannot be later than today's
    # Taiwan date; avoid probing unneeded future-year calendar coverage here.
    for offset in range(8):
        day = candidate + dt.timedelta(days=offset)
        if not _is_open(day, calendar):
            continue
        deadline = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6, 30), TAIPEI)
        if deadline <= now:
            found = day
    if found is None:
        raise CalendarUnavailable("no completed US session in verified calendar window")
    return found


def us_next_due(expected, calendar):
    session = _next_open(expected, calendar)
    return dt.datetime.combine(session + dt.timedelta(days=1), dt.time(6, 30), TAIPEI)


def market_contract(latest, expected, next_due, market_open_today, reason=None):
    if reason:
        return {
            "status": "unavailable", "latestSessionDate": latest.isoformat() if latest else None,
            "expectedSessionDate": expected.isoformat() if expected else None,
            "nextDueAt": next_due.isoformat() if next_due else None, "reasonCode": reason,
        }
    latest_text = latest.isoformat() if latest else None
    expected_text = expected.isoformat() if expected else None
    if latest != expected:
        return {
            "status": "stale", "latestSessionDate": latest_text,
            "expectedSessionDate": expected_text,
            "nextDueAt": next_due.isoformat() if next_due else None,
            "reasonCode": "MARKET_DATA_STALE",
        }
    return {
        "status": "fresh" if market_open_today else "market_closed",
        "latestSessionDate": latest_text, "expectedSessionDate": expected_text,
        "nextDueAt": next_due.isoformat() if next_due else None,
        "reasonCode": "OK" if market_open_today else "OFFICIAL_MARKET_CLOSED",
    }


def build_calendar_contract(calendars):
    checked_at = calendars["checkedAt"]
    def through(source):
        return f"{max(source['years'])}-12-31"
    return {
        "status": "verified",
        "version": calendars["sha256"][:16],
        "checkedAt": checked_at,
        "cacheUsed": bool(calendars.get("cacheUsed")),
        "twse": {
            "source": "TWSE",
            "checkedAt": checked_at,
            "coverageThrough": through(calendars["twse"]),
            "temporaryClosureSource": calendars.get("temporaryClosureSource", "unverified"),
            "temporaryClosureCount": sum(
                item.get("state") == "closed" for item in calendars.get("specialClosures", [])
            ),
        },
        "cboe": {"source": "Cboe", "checkedAt": checked_at, "coverageThrough": through(calendars["cboe"])},
    }
