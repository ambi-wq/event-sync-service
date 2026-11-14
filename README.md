# Event Sync

A small service that pulls meetings from two systems (a CRM export and a calendar export), figures out which
records are the same real meeting, and shows one merged list. For every field you can see which system it came
from and where the two systems disagree.

Python + FastAPI on the backend, one plain HTML page for the frontend. No database: there are only ~40 records, so
everything is reconciled once when the server starts and kept in memory.

## Run it

```bash
pip install -r requirements.txt
python main.py
```

Then open http://localhost:8000. The API docs are at http://localhost:8000/docs.

## Files

```
main.py            the API (3 endpoints) + serves the page
reconcile.py       all the logic: cleaning, duplicates, matching, comparing
index.html  the page
data/              the two JSON files
```

## API

| Endpoint | What it returns |
|---|---|
| `GET /api/meetings` | All merged meetings. Optional: `?source=both\|crm\|calendar`, `?conflicts=true` |
| `GET /api/meetings/{id}` | One meeting, e.g. `/api/meetings/CRM-1002+CAL-A2` |
| `GET /api/issues` | Bad data we found and what we did about it |

Each meeting has a `fields` object. Every field looks like:

```json
"location": {
  "value": "Zoom - https://zoom.us/j/98765432100",
  "source": "calendar",
  "crm": "NYC Office - 30th Floor",
  "calendar": "Zoom - https://zoom.us/j/98765432100",
  "status": "conflict",
  "note": "different places (or in-person vs virtual), calendar shown"
}
```

`status` is one of `same`, `similar` (same fact, one is just more detailed), `conflict`, or `one_source`
(only one system has that field). A meeting also has `conflicts` (list of conflicting field names), `sources`,
`match_score`, `match_reasons` and `problems`.

## How it works

`reconcile.py` does four things in order:

1. **Clean** every record into a simple dict. Bad data is fixed when it's safe and always reported, never silently dropped.
2. **Collapse duplicates** in the calendar.
3. **Match** CRM records to calendar events.
4. **Compare** the matched pair field by field.

### Matching
There's no shared ID, so each CRM record and calendar event get a score out of 100:

| What | Points |
|---|---|
| How close the start times are (more than 3 hours apart = never matched) | 35 |
| Company name in the calendar title or guest email domains, and/or the client is on the invite | 30 |
| CRM relationship owner is the calendar organizer | 15 |
| Locations don't contradict each other | 10 |
| Title words in common | 10 |

Pairs scoring 55 or more are matched, best score first, and each record is used only once. I used a score
instead of exact rules because real pairs often have very different titles, e.g. "Due Diligence Review" vs
"Crestview Holdings - DD Meeting".

### Duplicates
`CAL-A5` and `CAL-A6` (both Pinnacle, same organizer, 30 min apart, share a guest, similar title) are the same
meeting. I keep the one created first and add the other's guests to it. The weekly team syncs are on different
days, so they stay separate.

### Which value is shown when sources disagree
Both values are always kept next to each other, and the one shown is picked by a simple rule:

- **Calendar** wins for time, location and in-person/virtual (it's system-generated and has the real join link).
- **CRM** wins for status and owner (it's the relationship system).
- Titles are always worded differently, so that's never counted as a conflict.

What counts as a conflict: different start times (more than 5 min), in-person vs virtual, different places
("30th Floor" vs "12th Floor"), one side cancelled while the other is active, different owner. What does *not*:
"NYC Office" vs "NYC Office - 12th Floor" (one is just more detailed), or "Completed" vs "Confirmed".

### Bad data and what I did

| Problem | Handling |
|---|---|
| `CRM-1008` date is `03-15/2025` | read as month/day/year, flagged |
| `CRM-1007` has no time | matched on date only, calendar time used |
| `CRM-1017` client is "Multiple" | treated as empty, flagged |
| `CAL-A16` guest `raj.patel[at]atlasvc.com` | fixed to `@`, flagged |
| `CAL-A20` guest `external-guests` | not an email, ignored, flagged |
| `CAL-A11` no guests / location | flagged, still shown |
| `CAL-A4` times are UTC | converted to local time |
| Meetings in only one system | still shown, labelled "CRM only" / "Calendar only" |

## Assumptions

- **Timezone.** The files never say. Only `CAL-A4` has a timezone (`Z` = UTC), everything else is plain local time.
  I assume the plain times are `America/New_York` (the locations are DC, NYC and Boston) and convert UTC into it.
  That makes `CAL-A4` 15:00 vs the CRM's 14:00, so it shows as a start-time conflict (it's 14:00 if you use a fixed
  UTC-5). Change it with `EVENT_SYNC_TZ`, e.g. `EVENT_SYNC_TZ=Etc/GMT+5 python main.py`. I tried
  `Asia/Kolkata` too, but it moves `CAL-A4` to 00:30 the next day and it stops matching its CRM record, so it doesn't fit this data.
- Ambiguous dates like `03-15/2025` are month-first.
- "Internal" meetings are ones where every guest is `@firma.com`.

## Limitations

- Scoring weights are hand-tuned on this small dataset.
- Only the calendar is checked for duplicates (none in the CRM data).
- If two candidates score almost the same, it just takes the higher one; it doesn't flag the tie.
- Recurring events are treated as separate meetings.
- Data is read once at startup, so restart the server after editing the JSON files.

## Time spent

Approximately 5 hours.

## AI use

I used ChatGPT mainly during planning and development to discuss the reconciliation approach and explore edge cases. I also used it to review some code and suggest refactoring improvements. I didn't blindly accept the suggestions. I reviewed the changes, understood the logic, made adjustments where required, and validated the implementation myself..
