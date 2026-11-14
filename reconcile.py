"""
Reads the CRM and calendar JSON files and builds one list of unified meetings.

Steps:
  1. clean each record into a simple dict (fixing / flagging bad data)
  2. collapse duplicate calendar events
  3. match CRM records to calendar events with a score
  4. compare the two sides field by field and note any conflicts
"""
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path(__file__).parent / "data"

# The files never say what timezone they are in, so we assume one (see README).
TZ = ZoneInfo(os.getenv("EVENT_SYNC_TZ", "America/New_York"))
TZ_NAME = os.getenv("EVENT_SYNC_TZ", "America/New_York")

OUR_DOMAIN = "firma.com"
MATCH_THRESHOLD = 55       # a CRM record + calendar event need at least this score (out of 100)
MAX_GAP_MINUTES = 180      # events further apart than this are never matched
SAME_TIME_MINUTES = 5      # start times this close count as the same

NOISE_WORDS = {"the", "restaurant", "hq", "virtual"}   # ignored when comparing locations
GENERIC_COMPANY_WORDS = {"capital", "partners", "group", "holdings", "advisors", "investments",
                         "wealth", "ventures", "institutional", "fund", "point", "lp"}


# ---------------------------------------------------------------- small helpers

def words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def overlap(a, b):
    """How similar two titles are, 0..1 (shared words / words in the shorter title)."""
    wa, wb = words(a), words(b)
    return len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0


def name_from_email(email):
    return " ".join(p.capitalize() for p in re.split(r"[._-]", email.split("@")[0]))


def is_virtual(place):
    return bool(place and re.search(r"zoom|teams|webex|meet\b|virtual|https?://", place, re.I))


def place_words(place):
    place = re.sub(r"https?://\S+", " ", (place or "").lower())
    return words(place) - NOISE_WORDS


def compare_places(a, b):
    """'same', 'similar' (one is more detailed) or 'conflict'."""
    if is_virtual(a) != is_virtual(b):
        return "conflict"
    wa, wb = place_words(a), place_words(b)
    if wa == wb:
        return "same"
    if wa <= wb or wb <= wa:
        return "similar"
    return "conflict"


def to_local(text):
    """ISO text -> datetime in our timezone. 'Z' = UTC, no offset = already local time."""
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)


def clean_crm(rec, problems):
    rid = rec["crm_id"]
    flag = lambda msg: problems.append({"source": "crm", "record": rid, "problem": msg})

    # date: normally 2025-03-10, but one record uses 03-15/2025
    day, text = None, (rec.get("meeting_date") or "").strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        m = re.match(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", text)
        if m:
            day = date(int(m[3]), int(m[1]), int(m[2]))   # assume month first
            flag(f"date '{text}' is not ISO format, read as {day}")
    start, has_time = None, False
    if day is None:
        flag(f"meeting_date '{text}' is missing or unreadable")
    else:
        m = re.match(r"^(\d{1,2}):(\d{2})", rec.get("meeting_time") or "")
        if m:
            start = datetime(day.year, day.month, day.day, int(m[1]), int(m[2]), tzinfo=TZ)
            has_time = True
        else:
            start = datetime(day.year, day.month, day.day, tzinfo=TZ)
            flag("no meeting_time, matched on date only")

    client = rec.get("client_name")
    if client and client.lower() in ("multiple", "various", "tbd", "n/a"):
        flag(f"client_name '{client}' is a placeholder, treated as empty")
        client = None
    if not rec.get("location"):
        flag("no location")

    return {"source_id": rid, "title": rec.get("subject"), "start": start, "has_time": has_time,
            "location": rec.get("location"), "type": rec.get("meeting_type"),
            "status": (rec.get("status") or "").lower() or None, "owner": rec.get("relationship_owner"),
            "client": client, "company": rec.get("client_company"), "notes": rec.get("notes"),
            "raw": rec}


def clean_calendar(rec, problems):
    rid = rec["event_id"]
    flag = lambda msg: problems.append({"source": "calendar", "record": rid, "problem": msg})

    times = {}
    for key in ("start_time", "end_time"):
        try:
            times[key] = to_local(rec[key])
            if key == "start_time" and rec[key].strip().endswith("Z"):
                flag(f"times are in UTC (Z), converted to {TZ_NAME}")
        except (KeyError, ValueError, AttributeError):
            times[key] = None
            flag(f"{key} is missing or unreadable")

    attendees = []
    for a in rec.get("attendees") or []:
        fixed = a.strip().lower().replace("[at]", "@")
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", fixed):
            attendees.append(fixed)
            if fixed != a.strip().lower():
                flag(f"attendee '{a}' is malformed, fixed to '{fixed}'")
        else:
            flag(f"attendee '{a}' is not an email address, ignored")
    if not rec.get("attendees"):
        flag("no attendees")

    place = rec.get("location")
    if not place:
        flag("no location")
    internal = bool(attendees) and all(a.endswith("@" + OUR_DOMAIN) for a in attendees)
    mode = None if not place else ("virtual" if is_virtual(place) else "in-person")
    kind = "Internal" if internal else {"virtual": "Virtual", "in-person": "In-Person"}.get(mode)

    return {"source_id": rid, "title": rec.get("title"), "start": times["start_time"], "end": times["end_time"],
            "location": place, "type": kind, "internal": internal, "mode": mode,
            "status": (rec.get("status") or "").lower() or None,
            "organizer": rec.get("organizer"), "owner": name_from_email(rec["organizer"]) if rec.get("organizer") else None,
            "attendees": attendees, "notes": rec.get("description"), "created": rec.get("created_at") or "",
            "also_ids": [], "raw": rec}


# ---------------------------------------------------------------- step 2: duplicates

def looks_like_duplicate(a, b):
    if not a["start"] or not b["start"] or a["owner"] != b["owner"]:
        return False
    close = abs(a["start"] - b["start"]) <= timedelta(minutes=60)
    shared = (set(a["attendees"]) & set(b["attendees"])) - {a["organizer"]}
    return close and bool(shared) and overlap(a["title"], b["title"]) >= 0.4


def collapse_duplicates(events, problems):
    """Same organizer, close start, shared guest and similar title = same meeting.
    We keep the earliest-created one and merge the guests of the others into it."""
    kept = []
    for e in sorted(events, key=lambda e: e["created"]):
        original = next((k for k in kept if looks_like_duplicate(k, e)), None)
        if original is None:
            kept.append(e)
            continue
        original["also_ids"].append(e["source_id"])
        original["attendees"] += [a for a in e["attendees"] if a not in original["attendees"]]
        problems.append({"source": "calendar", "record": e["source_id"],
                         "problem": f"looks like a duplicate of {original['source_id']}, merged into it"})
    return kept


def score(crm, cal):
    """Score (0-100) for 'these two are the same meeting'. Returns (score, reasons) or None."""
    if not crm["start"] or not cal["start"]:
        return None
    reasons, total = [], 0

    # time (35 pts)
    if crm["has_time"]:
        gap = abs((crm["start"] - cal["start"]).total_seconds()) / 60
        if gap > MAX_GAP_MINUTES:
            return None
        pts = 35 * (1 - gap / MAX_GAP_MINUTES)
        reasons.append(f"starts {gap:.0f} min apart")
    else:
        if crm["start"].date() != cal["start"].date():
            return None
        pts = 14
        reasons.append("same day (CRM has no time)")
    total += pts

    # who is it with (30 pts): company name in the title / guest email domains, or the client is on the invite
    title = (cal["title"] or "").lower()
    domains = " ".join(a.split("@")[1] for a in cal["attendees"])
    who = 0
    if any(w in title or w in domains for w in words(crm["company"]) - GENERIC_COMPANY_WORDS if len(w) > 2):
        who += 25
        reasons.append(f"company '{crm['company']}' found in title or guest emails")
    if crm["client"]:
        client = " ".join(sorted(words(crm["client"])))
        names = [" ".join(sorted(words(a.split("@")[0]))) for a in cal["attendees"]]
        if client in names or crm["client"].lower() in title:
            who += 15
            reasons.append(f"client {crm['client']} is on the invite")
    if not who and crm["type"] == "Internal" and cal["internal"]:
        who = 10
        reasons.append("both are internal meetings")
    total += min(who, 30)

    # owner (15 pts)
    if crm["owner"] == cal["owner"]:
        total += 15
        reasons.append(f"{crm['owner']} is the organizer")
    elif crm["owner"] and crm["owner"].lower().replace(" ", ".") in [a.split("@")[0] for a in cal["attendees"]]:
        total += 7
        reasons.append(f"{crm['owner']} is a guest")

    # location (10 pts) and title (10 pts)
    if crm["location"] and cal["location"] and compare_places(crm["location"], cal["location"]) != "conflict":
        total += 10
        reasons.append("locations agree")
    total += 10 * overlap(crm["title"], cal["title"])

    return round(total, 1), reasons


def match(crm_list, cal_list):
    """Best-scoring pairs first; each record can only be used once."""
    scored = []
    for c in crm_list:
        for e in cal_list:
            result = score(c, e)
            if result and result[0] >= MATCH_THRESHOLD:
                scored.append((result[0], c, e, result[1]))
    scored.sort(key=lambda s: -s[0])
    pairs, used_crm, used_cal = [], set(), set()
    for s, c, e, reasons in scored:
        if c["source_id"] in used_crm or e["source_id"] in used_cal:
            continue
        used_crm.add(c["source_id"])
        used_cal.add(e["source_id"])
        pairs.append((c, e, s, reasons))
    return pairs, used_crm, used_cal



def field(value, source, crm, cal, status, note=""):
    """One field of a unified meeting.
    status: same / similar (same fact, different detail) / conflict / one_source / empty"""
    return {"value": value, "source": source, "crm": crm, "calendar": cal, "status": status, "note": note}


def one_side(crm_val, cal_val):
    if crm_val is not None:
        return field(crm_val, "crm", crm_val, None, "one_source")
    if cal_val is not None:
        return field(cal_val, "calendar", None, cal_val, "one_source")
    return field(None, None, None, None, "empty")


def iso(dt):
    return dt.isoformat() if dt else None


def compare(crm, cal):
    """Build every field of the unified meeting. When both sides have a value we compare them.
    Rule of thumb for what is shown on conflict: calendar for time/place, CRM for status/owner."""
    f = {}
    c = crm or {}
    e = cal or {}

    # title: wording always differs between systems, so never a conflict
    if crm and cal:
        same = c["title"].lower() == e["title"].lower()
        f["title"] = field(e["title"], "calendar", c["title"], e["title"], "same" if same else "similar",
                           "" if same else "titles are worded differently, calendar title shown")
    else:
        f["title"] = one_side(c.get("title"), e.get("title"))

    # start
    crm_start = iso(c.get("start")) if c.get("has_time") else (c["start"].date().isoformat() if c.get("start") else None)
    cal_start = iso(e.get("start"))
    if crm and cal and c["start"] and e["start"]:
        if not c["has_time"]:
            f["start"] = field(cal_start, "calendar", crm_start, cal_start, "similar", "CRM only has a date")
        else:
            gap = abs((c["start"] - e["start"]).total_seconds()) / 60
            if gap <= SAME_TIME_MINUTES:
                f["start"] = field(cal_start, "both", crm_start, cal_start, "same")
            else:
                f["start"] = field(cal_start, "calendar", crm_start, cal_start, "conflict",
                                   f"start times are {gap:.0f} min apart, calendar time shown")
    else:
        f["start"] = one_side(crm_start, cal_start)
    f["end"] = one_side(None, iso(e.get("end")))

    # location
    if c.get("location") and e.get("location"):
        result = compare_places(c["location"], e["location"])
        if result == "conflict":
            f["location"] = field(e["location"], "calendar", c["location"], e["location"], "conflict",
                                  "different places (or in-person vs virtual), calendar shown")
        else:
            longer = max(c["location"], e["location"], key=lambda p: (len(place_words(p)), "http" in p, len(p)))
            f["location"] = field(longer, "calendar" if longer == e["location"] else "crm", c["location"],
                                  e["location"], result, "" if result == "same" else "one is more detailed, that one shown")
    else:
        f["location"] = one_side(c.get("location"), e.get("location"))

    # meeting type: CRM says it directly, for the calendar we work it out from guests + location
    if c.get("type") and e.get("type"):
        if c["type"] == "Internal":
            clash = e["internal"] is False and bool(e["attendees"])
            why = "calendar invite has outside guests"
        else:
            clash = e["mode"] is not None and (c["type"] == "Virtual") != (e["mode"] == "virtual")
            why = f"CRM says {c['type']} but calendar location looks {e['mode']}"
        if clash:
            f["type"] = field(e["type"], "calendar", c["type"], e["type"], "conflict", why + ", calendar shown")
        else:
            f["type"] = field(c["type"], "both", c["type"], e["type"], "same" if c["type"] == e["type"] else "similar")
    else:
        f["type"] = one_side(c.get("type"), e.get("type"))

    # status
    groups = {"confirmed": "active", "scheduled": "active", "completed": "active",
              "tentative": "tentative", "cancelled": "cancelled", "canceled": "cancelled"}
    if c.get("status") and e.get("status"):
        if c["status"] == e["status"]:
            f["status"] = field(c["status"], "both", c["status"], e["status"], "same")
        elif groups.get(c["status"]) == groups.get(e["status"]):
            f["status"] = field(c["status"], "crm", c["status"], e["status"], "similar")
        else:
            note = "CRM status shown"
            if "cancelled" in (groups.get(c["status"]), groups.get(e["status"])):
                note = "one system says cancelled, the other doesn't - needs a human check. CRM status shown"
            f["status"] = field(c["status"], "crm", c["status"], e["status"], "conflict", note)
    else:
        f["status"] = one_side(c.get("status"), e.get("status"))

    # owner
    if c.get("owner") and e.get("owner"):
        same = c["owner"] == e["owner"]
        f["owner"] = field(c["owner"], "both" if same else "crm", c["owner"], e["owner"],
                           "same" if same else "conflict", "" if same else "CRM owner and calendar organizer differ")
    else:
        f["owner"] = one_side(c.get("owner"), e.get("owner"))

    # fields only one system has
    f["client"] = one_side(c.get("client"), None)
    f["company"] = one_side(c.get("company"), None)
    f["attendees"] = one_side(None, e.get("attendees") or None)
    f["crm_notes"] = one_side(c.get("notes"), None)
    f["calendar_description"] = one_side(None, e.get("notes"))
    return f


def make_meeting(crm, cal, score_value=None, reasons=None):
    fields = compare(crm, cal)
    ids = [x["source_id"] for x in (crm, cal) if x]
    sources = [name for name, x in (("crm", crm), ("calendar", cal)) if x]
    return {
        "id": "+".join(ids),
        "title": fields["title"]["value"],
        "start": fields["start"]["value"],
        "sources": sources,
        "calendar_ids": ([cal["source_id"]] + cal["also_ids"]) if cal else [],
        "fields": fields,
        "conflicts": [name for name, v in fields.items() if v["status"] == "conflict"],
        "match_score": score_value,
        "match_reasons": reasons or [f"only in {sources[0]}, no counterpart found"],
        "problems": [],
        "raw": {"crm": crm["raw"] if crm else None, "calendar": cal["raw"] if cal else None},
    }


def build():
    problems = []
    crm_list = [clean_crm(r, problems) for r in json.load(open(DATA / "crm_events.json", encoding="utf-8"))]
    cal_list = [clean_calendar(r, problems) for r in json.load(open(DATA / "calendar_events.json", encoding="utf-8"))]
    cal_list = collapse_duplicates(cal_list, problems)

    pairs, used_crm, used_cal = match(crm_list, cal_list)
    meetings = [make_meeting(c, e, s, why) for c, e, s, why in pairs]
    meetings += [make_meeting(c, None) for c in crm_list if c["source_id"] not in used_crm]
    meetings += [make_meeting(None, e) for e in cal_list if e["source_id"] not in used_cal]
    meetings.sort(key=lambda m: (m["start"] is None, m["start"] or ""))

    # attach each problem to the meeting it belongs to
    owner_of = {i: m for m in meetings for i in m["id"].split("+") + m["calendar_ids"]}
    for p in problems:
        m = owner_of.get(p["record"])
        p["meeting_id"] = m["id"] if m else None
        if m:
            m["problems"].append(p["problem"])
    return {"meetings": meetings, "problems": problems}
