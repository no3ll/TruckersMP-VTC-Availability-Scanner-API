from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# -------------------------------------------------
# CORS CONFIG
# -------------------------------------------------

# ⚠️ You can adjust these if your local / GitHub URLs are different.
# See explanation after the code.
origins = [
    "http://127.0.0.1:5500",  # VS Code Live Server default
    "http://localhost:5500",  # local alt
    "https://no3ll.github.io",  # your GitHub Pages user root
    "https://no3ll.github.io/TruckersMP-VTC-Availability-Scanner",  # project page
]

app = FastAPI(title="TruckersMP VTC Availability Scanner API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------
# MODELS
# -------------------------------------------------


class ScanRequest(BaseModel):
    event_urls: List[str]


class VTCEntry(BaseModel):
    id: int
    name: str | None = None
    status: str | None = None  # VERIFIED / VALIDATED / NORMAL / etc.
    recruitment: str | None = None
    tmp_url: str | None = None
    discord_url: str | None = None
    # Any extra fields are preserved but not validated strictly
    extra: Dict[str, Any] | None = None


class ScanResponse(BaseModel):
    busy_vtc_ids: List[int]
    free_vtcs: List[VTCEntry]


# -------------------------------------------------
# LOAD VTC DATABASE (vtcs_source.json)
# -------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
VTC_DB_PATH = BASE_DIR / "vtcs_source.json"


def load_vtc_db() -> Dict[int, Dict[str, Any]]:
    if not VTC_DB_PATH.exists():
        print(f"[WARN] {VTC_DB_PATH} not found. Using empty DB.")
        return {}

    with VTC_DB_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    result: Dict[int, Dict[str, Any]] = {}
    if isinstance(data, list):
        for raw in data:
            if not isinstance(raw, dict):
                continue
            raw_id = raw.get("id")
            if raw_id is None:
                continue
            try:
                vtc_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            result[vtc_id] = raw
    elif isinstance(data, dict):
        # If you ever store as { "123": {...}, ... }
        for key, raw in data.items():
            try:
                vtc_id = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(raw, dict):
                result[vtc_id] = raw

    print(f"[INFO] Loaded {len(result)} VTCs from {VTC_DB_PATH}")
    return result


VTC_DB: Dict[int, Dict[str, Any]] = load_vtc_db()


# -------------------------------------------------
# SCRAPING HELPERS
# -------------------------------------------------


async def fetch_html(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, timeout=20)
    resp.raise_for_status()
    return resp.text


def parse_vtcs_from_event_html(html: str) -> Set[int]:
    """
    Parse TruckersMP event HTML and extract VTC IDs from links like /vtc/123-foo.
    This function is written to be type-safe so Pylance doesn't complain.
    """
    soup = BeautifulSoup(html, "lxml")
    ids: Set[int] = set()

    for a in soup.find_all("a"):
        href_val = a.get("href")

        # href_val can be AttributeValueList | str | None.
        # Normalize to plain str safely so Pylance is happy.
        if href_val is None:
            continue

        href = str(href_val)

        if "/vtc/" not in href:
            continue

        # Try to extract numeric ID right after '/vtc/'
        try:
            after = href.split("/vtc/", 1)[1]
        except IndexError:
            continue

        # after might be "1234-some-name", "1234", "1234?x=y", etc.
        id_str = ""
        for ch in after:
            if ch.isdigit():
                id_str += ch
            else:
                break

        if not id_str:
            continue

        try:
            vtc_id = int(id_str)
        except ValueError:
            continue

        ids.add(vtc_id)

    return ids


async def collect_busy_vtcs(event_urls: List[str]) -> Set[int]:
    busy: Set[int] = set()
    async with httpx.AsyncClient(headers={"User-Agent": "TruckersMP-VTC-Scanner/1.0"}) as client:
        for url in event_urls:
            url = url.strip()
            if not url:
                continue
            try:
                html = await fetch_html(client, url)
            except httpx.HTTPError as e:
                print(f"[WARN] Failed to fetch {url}: {e}")
                continue

            vtc_ids = parse_vtcs_from_event_html(html)
            print(f"[DEBUG] Event {url} -> found {len(vtc_ids)} VTC(s)")
            busy.update(vtc_ids)

    return busy


def map_vtc_dict_to_entry(vtc_id: int, raw: Dict[str, Any]) -> VTCEntry:
    """
    Convert the raw dict from vtcs_source.json to our Pydantic VTCEntry.
    This keeps known fields and stuffs the rest into 'extra'.
    """
    name = raw.get("name")
    status = raw.get("status")
    recruitment = raw.get("recruitment")
    tmp_url = raw.get("tmp_url") or raw.get("truckersmp_url") or raw.get("url")
    discord_url = raw.get("discord") or raw.get("discord_url")

    # Put all other keys into 'extra'
    extra_keys = {"id", "name", "status", "recruitment", "tmp_url", "truckersmp_url", "url", "discord", "discord_url"}
    extra = {k: v for k, v in raw.items() if k not in extra_keys}

    return VTCEntry(
        id=vtc_id,
        name=name,
        status=status,
        recruitment=recruitment,
        tmp_url=tmp_url,
        discord_url=discord_url,
        extra=extra or None,
    )


# -------------------------------------------------
# ROUTES
# -------------------------------------------------


@app.get("/")
async def root() -> Dict[str, str]:
    return {"status": "ok", "message": "TruckersMP VTC Availability Scanner API"}


@app.post("/scan", response_model=ScanResponse)
async def scan_event_vtcs(payload: ScanRequest) -> ScanResponse:
    if not payload.event_urls:
        raise HTTPException(status_code=400, detail="event_urls list cannot be empty")

    busy_ids = await collect_busy_vtcs(payload.event_urls)

    # Free = in DB but not in busy_ids
    free_vtcs: List[VTCEntry] = []
    for vtc_id, raw in VTC_DB.items():
        if vtc_id in busy_ids:
            continue
        entry = map_vtc_dict_to_entry(vtc_id, raw)
        free_vtcs.append(entry)

    # You can also sort by status, name, etc here if you want.
    free_vtcs.sort(key=lambda v: (v.status or "ZZZ", (v.name or "").lower()))

    return ScanResponse(
        busy_vtc_ids=sorted(busy_ids),
        free_vtcs=free_vtcs,
    )
