"""Event Sync API.  Run with:  python main.py   (then open http://localhost:8000)"""
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import reconcile

app = FastAPI(title="Event Sync")

# Only ~40 records, so we just reconcile once at startup and keep it in memory.
DATA = reconcile.build()


@app.get("/api/meetings")
def list_meetings(source: str = None, conflicts: bool = False):
    """All unified meetings, soonest first.
    ?source=both | crm | calendar   -> where the meeting exists
    ?conflicts=true                 -> only meetings where the sources disagree"""
    meetings = DATA["meetings"]
    if source == "both":
        meetings = [m for m in meetings if len(m["sources"]) == 2]
    elif source in ("crm", "calendar"):
        meetings = [m for m in meetings if m["sources"] == [source]]
    if conflicts:
        meetings = [m for m in meetings if m["conflicts"]]
    return meetings


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: str):
    """One meeting, e.g. /api/meetings/CRM-1002+CAL-A2"""
    for m in DATA["meetings"]:
        if m["id"] == meeting_id:
            return m
    raise HTTPException(status_code=404, detail="meeting not found")


@app.get("/api/issues")
def list_issues():
    """Bad or odd data we found (and what we did about it)."""
    return DATA["problems"]


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "index.html")


if __name__ == "__main__":
    uvicorn.run("main:app", port=8000)
