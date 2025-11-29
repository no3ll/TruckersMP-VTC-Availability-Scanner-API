import json
import re
from typing import List, Optional, Dict, Any, Set

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ----- Load VTC database once on startup -----

VTC_DB: List[Dict[str, Any]] = []

try:
    with open("vtcs_source.json", "r", encoding="utf-8") as f:
        data = json.load(f)
        # support both [ {...}, {...} ] and { "vtcs": [ ... ] }
        if isinstance(data, list):
            VTC_DB = data
        elif isinstance(data, dict) and isinstance(data.get("vtcs"), list):
            VTC_DB = data["vtcs"]
        else:
            print("[WARN] Unexpected vtcs_source.json structure, using empty DB.")
            VTC_DB = []
    print(f"[INFO] Loaded {len(VTC_DB)} VTC(s) from vtcs_source.json")
except FileNotFoundError:
    print("[WARN] vtcs_source.json not found; VTC_DB is empty.")
    VTC_DB = []


# ----- Models -----

class Filters(BaseModel):
    verified: bool = True
    validated: bool = True
    normal: bool = True
    recruitmentOpenOnly: bool = True


class ScanRequest(BaseModel):
    event_urls: List[str]
    filters: Optional[Filters] = None


class ScanResponse(BaseModel):
    busy_vtc_ids: List[int]
    invite_vtcs: List[Dict[str, Any]]
    counts: Dict[str, Any]


# ----- Helpers -----

EVENT_ID_RE = re.compile(r"events/(\d+)", re.IGNORECASE)
VTC_ID_RE = re.compile(r"/vtc/(\d+)")


def extract_event_ids(urls: List[str]) -> List[int]:
    ids: Set[int] = set()
    for raw in urls:
        match = EVENT_ID_RE.search(raw)
        if match:
            num = int(match.group(1))
            ids.add(num)
    return sorted(ids)


async def fetch_vtc_ids_for_event(client: httpx.AsyncClient, event_id: int) -> Set[int]:
    url = f"https://truckersmp.com/events/{event_id}"
    try:
        resp = await client.get(url, timeout=15.0)
        if resp.status_code != 200:
            print(f"[WARN] Failed to fetch {url}: {resp.status_code}")
            return set()
        html = resp.text
        ids: Set[int] = set()
        for m in VTC_ID_RE.finditer(html):
            ids.add(int(m.group(1)))
        return ids
    except Exception as e:
        print(f"[ERROR] Exception while fetching {url}: {e}")
        return set()


async def collect_busy_vtcs(event_ids: List[int]) -> Set[int]:
    busy: Set[int] = set()
    async with httpx.AsyncClient() as client:
        for eid in event_ids:
            ids = await fetch_vtc_ids_for_event(client, eid)
            busy.update(ids)
    return busy


STATUS_ORDER = {"verified": 0, "validated": 1, "normal": 2}


def filter_and_sort_vtcs(busy_ids: Set[int], filters: Filters) -> List[Dict[str, Any]]:
    def passes_filters(vtc: Dict[str, Any]) -> bool:
        try:
            vtc_id = int(vtc.get("id", 0))
        except Exception:
            return False

        if vtc_id in busy_ids:
            return False

        status = str(vtc.get("status", "normal")).lower()
        if status == "verified" and not filters.verified:
            return False
        if status == "validated" and not filters.validated:
            return False
        if status == "normal" and not filters.normal:
            return False

        if filters.recruitmentOpenOnly:
            recruitment = str(vtc.get("recruitment", "")).upper()
            if recruitment != "OPEN":
                return False

        return True

    filtered = [vtc for vtc in VTC_DB if passes_filters(vtc)]

    def sort_key(vtc: Dict[str, Any]):
        status = str(vtc.get("status", "normal")).lower()
        status_rank = STATUS_ORDER.get(status, 99)
        members = 0
        try:
            members = int(vtc.get("members", 0))
        except Exception:
            pass
        name = str(vtc.get("name", "")).lower()
        return (status_rank, -members, name)

    filtered.sort(key=sort_key)
    return filtered


# ----- FastAPI app -----

app = FastAPI(title="TruckersMP VTC Availability API")


@app.get("/")
async def root():
    return {"status": "ok", "message": "TruckersMP VTC Availability API"}


@app.post("/scan", response_model=ScanResponse)
async def scan_vtcs(req: ScanRequest):
    if not req.event_urls:
        raise HTTPException(status_code=400, detail="event_urls cannot be empty")

    filters = req.filters or Filters()

    event_ids = extract_event_ids(req.event_urls)
    if not event_ids:
        raise HTTPException(status_code=400, detail="No valid event IDs found")

    busy_ids = await collect_busy_vtcs(event_ids)
    invite_vtcs = filter_and_sort_vtcs(busy_ids, filters)

    counts = {
        "events_scanned": len(event_ids),
        "busy_vtcs": len(busy_ids),
        "total_vtcs_in_db": len(VTC_DB),
        "invite_ready_vtcs": len(invite_vtcs),
    }

    return ScanResponse(
        busy_vtc_ids=sorted(busy_ids),
        invite_vtcs=invite_vtcs,
        counts=counts,
    )
