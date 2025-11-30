import json
import os
import re
from typing import Any, Dict, List, Set

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# -----------------------------
# FastAPI app + CORS
# -----------------------------

app = FastAPI(
    title="TruckersMP VTC Availability Scanner API",
    version="1.0.0",
)

# Allow your GitHub Pages site to call this API
# Replace this with your real pages URL once you know it
# e.g. "https://no3ll.github.io"
ALLOWED_ORIGINS = [
    "*",  # you can tighten this later
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Models
# -----------------------------

class ScanRequest(BaseModel):
    event_urls: List[str]
    status_filter: str = "any"          # "any", "verified", "verified_validated", "normal"
    recruitment_filter: str = "open"    # "open" or "any"


class ScanResultVTC(BaseModel):
    id: int
    name: str
    status: str
    recruitment: str | None = None
    discord: str | None = None
    tmp_url: str
    logo: str | None = None


class ScanResponse(BaseModel):
    busy_vtc_ids: List[int]
    free_vtcs: List[ScanResultVTC]
    total_vtcs_in_db: int


# -----------------------------
# Load VTC database
# -----------------------------

VTCS: Dict[int, Dict[str, Any]] = {}


def load_vtc_db() -> None:
    """
    Load vtcs_source.json into memory as {id: vtc_dict}.
    This runs once at startup.
    """
    global VTCS
    base_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(base_dir, "vtcs_source.json")

    if not os.path.exists(json_path):
        print("WARNING: vtcs_source.json not found in backend.")
        VTCS = {}
        return

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load vtcs_source.json: {e}")
        VTCS = {}
        return

    vtcs: Dict[int, Dict[str, Any]] = {}
    for raw in data:
        vid_raw = raw.get("id")
        try:
            if vid_raw is None:
                continue
            vid = int(vid_raw)
        except (ValueError, TypeError):
            continue
        vtcs[vid] = raw

    VTCS = vtcs
    print(f"[STARTUP] Loaded {len(VTCS)} VTCs from vtcs_source.json")


@app.on_event("startup")
def on_startup() -> None:
    load_vtc_db()


# -----------------------------
# Scraping helpers
# -----------------------------

async def fetch_event_vtc_ids(event_url: str) -> Set[int]:
    """
    Given a TruckersMP event URL, download the HTML and extract VTC IDs
    from links like /vtc/12345.
    """
    event_url = event_url.strip()
    if not event_url:
        return set()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(event_url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "lxml")
    ids: Set[int] = set()

    for a in soup.find_all("a", href=True):
        href_val = a.get("href")
        # BeautifulSoup types this as "AttributeValue", so guard it
        if not isinstance(href_val, str):
            continue

        m = re.search(r"/vtc/(\d+)", href_val)
        if m:
            try:
                ids.add(int(m.group(1)))
            except ValueError:
                continue

    return ids


def passes_status_filter(vtc: Dict[str, Any], status_filter: str) -> bool:
    status = str(vtc.get("status", "normal")).lower()

    if status_filter == "verified":
        return status == "verified"

    if status_filter == "verified_validated":
        return status in {"verified", "validated"}

    if status_filter == "normal":
        return status == "normal"

    # "any"
    return True


def passes_recruitment_filter(vtc: Dict[str, Any], recruitment_filter: str) -> bool:
    if recruitment_filter == "any":
        return True

    rec = vtc.get("recruitment")
    if not isinstance(rec, str):
        return False

    return rec.strip().upper() == "OPEN"


# -----------------------------
# Routes
# -----------------------------

@app.get("/")
def root() -> Dict[str, str]:
    """
    Simple root so hitting the Render URL in a browser shows something
    instead of 404.
    """
    return {
        "message": "TruckersMP VTC Availability Scanner API",
        "docs": "/docs",
        "scan_endpoint": "/api/scan",
    }


@app.post("/api/scan", response_model=ScanResponse)
async def scan_vtcs(req: ScanRequest) -> ScanResponse:
    """
    Main endpoint used by the frontend.
    Body example:
    {
      "event_urls": ["https://truckersmp.com/events/123", "..."],
      "status_filter": "verified_validated",
      "recruitment_filter": "open"
    }
    """
    if not VTCS:
        raise HTTPException(
            status_code=500,
            detail="vtcs_source.json is not loaded on the server.",
        )

    # 1) Find busy VTC IDs across all events
    busy_ids: Set[int] = set()

    for url in req.event_urls:
        url = url.strip()
        if not url:
            continue
        try:
            ids = await fetch_event_vtc_ids(url)
        except httpx.HTTPError:
            # Just skip broken URLs
            continue
        busy_ids |= ids

    # 2) Build list of free VTCs matching filters
    free_vtcs: List[ScanResultVTC] = []

    for vid, vtc in VTCS.items():
        if vid in busy_ids:
            continue
        if not passes_status_filter(vtc, req.status_filter):
            continue
        if not passes_recruitment_filter(vtc, req.recruitment_filter):
            continue

        name = str(vtc.get("name", "Unknown VTC"))
        status = str(vtc.get("status", "normal"))
        recruitment = vtc.get("recruitment")
        discord = vtc.get("discord")
        logo = vtc.get("logo")
        tmp_url = vtc.get("truckersmp_url") or f"https://truckersmp.com/vtc/{vid}"

        free_vtcs.append(
            ScanResultVTC(
                id=vid,
                name=name,
                status=status,
                recruitment=recruitment,
                discord=discord,
                tmp_url=tmp_url,
                logo=logo,
            )
        )

    # Sort: verified → validated → normal, then by name
    def sort_key(v: ScanResultVTC):
        st = v.status.lower()
        rank = 2
        if st == "verified":
            rank = 0
        elif st == "validated":
            rank = 1
        return (rank, v.name.lower())

    free_vtcs.sort(key=sort_key)

    return ScanResponse(
        busy_vtc_ids=sorted(list(busy_ids)),
        free_vtcs=free_vtcs,
        total_vtcs_in_db=len(VTCS),
    )
