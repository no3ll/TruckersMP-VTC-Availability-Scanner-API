from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set

import json

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---------- CONFIG ----------

HERE = Path(__file__).resolve().parent
VTCS_SOURCE_PATH = HERE / "vtcs_source.json"

# ⬅️ IMPORTANT: set this to the origin of your GitHub Pages site
# For example, if your site is at:
#   https://no3ll.github.io/TruckersMP-VTC-Availability-Scanner
# then the origin is:
#   https://no3ll.github.io
ALLOWED_ORIGINS = [
    "http://localhost:5500",           # local (if you use Live Server)
    "http://127.0.0.1:5500",
    "http://localhost:8000",           # optional
    "http://127.0.0.1:8000",
    "https://no3ll.github.io",         # GitHub Pages origin
]

# ---------- DATA MODELS ----------


class ScanRequest(BaseModel):
    event_urls: List[str]
    status_filter: Literal["all", "verified", "verified+validated", "normal"] = "all"
    recruitment_only: bool = True


class ScanResult(BaseModel):
    busy_vtc_ids: List[int]
    total_busy: int
    total_candidates: int
    total_available: int
    groups: Dict[str, List[Dict[str, Any]]]


# ---------- LOAD VTC DATABASE ----------

def load_vtcs_source() -> List[Dict[str, Any]]:
    if not VTCS_SOURCE_PATH.exists():
        raise RuntimeError(f"vtcs_source.json not found at {VTCS_SOURCE_PATH}")
    with VTCS_SOURCE_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise RuntimeError("vtcs_source.json must contain a list of VTC entries")
    return data


VTCS_DB: List[Dict[str, Any]] = load_vtcs_source()


# ---------- SCRAPING HELPERS ----------

def extract_vtc_ids_from_event_html(html: str) -> Set[int]:
    """
    Parse a TruckersMP event page and return all unique VTC IDs found in /vtc/<id> links.
    """
    soup = BeautifulSoup(html, "html.parser")
    vtc_ids: Set[int] = set()

    for a in soup.find_all("a"):
        href_val = a.get("href")
        # Make Pylance happy: always cast to string, handle None
        href = str(href_val) if href_val is not None else ""
        if "/vtc/" in href:
            # Expect formats like /vtc/12345 or https://truckersmp.com/vtc/12345
            parts = href.split("/vtc/")
            if len(parts) < 2:
                continue
            tail = parts[1]
            # Strip extra path/query stuff
            tail = tail.split("/")[0].split("?")[0].split("#")[0]
            try:
                vtc_id = int(tail)
                vtc_ids.add(vtc_id)
            except ValueError:
                continue

    return vtc_ids


def fetch_attending_vtcs_for_event(event_url: str) -> Set[int]:
    """
    Fetch a TruckersMP event page and return attending VTC IDs by scraping.
    """
    try:
        resp = httpx.get(event_url, timeout=20.0)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Error fetching {event_url}: {e}") from e

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Failed to fetch event {event_url} (HTTP {resp.status_code})",
        )

    return extract_vtc_ids_from_event_html(resp.text)


# ---------- FILTERING LOGIC ----------

def filter_vtcs(
    vtcs: List[Dict[str, Any]],
    busy_ids: Set[int],
    status_filter: str,
    recruitment_only: bool,
) -> ScanResult:
    # Busy VTCs (from events)
    busy_vtc_ids_sorted = sorted(busy_ids)

    # Start from all known VTCs that are NOT busy
    candidates = [v for v in vtcs if int(v.get("id", -1)) not in busy_ids]

    # Apply status filter for availability
    def status_ok(v: Dict[str, Any]) -> bool:
        status = str(v.get("status", "normal")).lower()
        if status_filter == "all":
            return True
        if status_filter == "verified":
            return status == "verified"
        if status_filter == "verified+validated":
            return status in ("verified", "validated")
        if status_filter == "normal":
            return status == "normal"
        return True

    # Apply recruitment filter
    def recruitment_ok(v: Dict[str, Any]) -> bool:
        if not recruitment_only:
            return True
        recruitment = str(v.get("recruitment", "")).upper()
        return recruitment == "OPEN"

    filtered = [v for v in candidates if status_ok(v) and recruitment_ok(v)]

    # Group (for UI sections)
    groups: Dict[str, List[Dict[str, Any]]] = {"verified": [], "validated": [], "normal": []}
    for v in filtered:
        status = str(v.get("status", "normal")).lower()
        if status == "verified":
            groups["verified"].append(v)
        elif status == "validated":
            groups["validated"].append(v)
        else:
            groups["normal"].append(v)

    return ScanResult(
        busy_vtc_ids=busy_vtc_ids_sorted,
        total_busy=len(busy_ids),
        total_candidates=len(candidates),
        total_available=len(filtered),
        groups=groups,
    )


# ---------- FASTAPI APP ----------

app = FastAPI(title="TruckersMP VTC Availability Scanner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/scan-events", response_model=ScanResult)
def scan_events(payload: ScanRequest) -> ScanResult:
    if not payload.event_urls:
        raise HTTPException(status_code=400, detail="event_urls list cannot be empty")

    all_busy: Set[int] = set()
    for url in payload.event_urls:
        url = url.strip()
        if not url:
            continue
        event_busy = fetch_attending_vtcs_for_event(url)
        all_busy.update(event_busy)

    result = filter_vtcs(
        vtcs=VTCS_DB,
        busy_ids=all_busy,
        status_filter=payload.status_filter,
        recruitment_only=payload.recruitment_only,
    )
    return result
