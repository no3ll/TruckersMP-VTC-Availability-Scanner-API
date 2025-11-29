import json
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup

app = FastAPI()

# ----------------------------
# CORS SETUP
# ----------------------------
origins = [
    "http://localhost",
    "http://127.0.0.1:5500",  # VS Code Live Server
    "http://localhost:5500",
    "http://127.0.0.1:8000",
    # GitHub Pages root
    "https://no3ll.github.io",
    # GitHub pages project folder
    "https://no3ll.github.io/TruckersMP-VTC-Availability-Scanner"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------
# HELPERS
# ----------------------------

async def scrape_event_vtcs(event_url: str):
    """Scrapes attending VTCs from a TruckersMP event page."""
    print(f"[DEBUG] Scraping {event_url}")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(event_url)

        soup = BeautifulSoup(response.text, "lxml")

        # All VTC links contain: /vtc/<id>
        vtc_links = soup.select("a[href*='/vtc/']")

        vtc_ids = set()
        for tag in vtc_links:
            href = tag.get("href", "")
            if "/vtc/" in href:
                try:
                    vtc_id = int(href.split("/vtc/")[1].split("/")[0])
                    vtc_ids.add(vtc_id)
                except:
                    continue

        return list(vtc_ids)

    except Exception as e:
        print("[ERROR] scrape_event_vtcs:", e)
        return []


def load_vtc_database():
    """Load vtcs_source.json bundled with the API service."""
    try:
        with open("vtcs_source.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("[ERROR] Failed to load VTC database:", e)
        return []


# ----------------------------
# API ROUTES
# ----------------------------

@app.get("/")
def home():
    return {"message": "TruckersMP VTC Availability Scanner API is running 🚚"}


@app.post("/check_vtcs/")
async def check_vtcs(payload: dict):
    """
    Input:
    {
        "event_urls": [ "https://truckersmp.com/events/xxxx", ... ]
    }

    Output:
    {
        "busy": [...],
        "free": [...],
        "total_free": 123
    }
    """
    event_urls = payload.get("event_urls", [])
    all_busy = set()

    # 1. scrape each event
    for url in event_urls:
        vtcs = await scrape_event_vtcs(url)
        all_busy.update(vtcs)

    busy_list = list(all_busy)

    # 2. load DB
    full_db = load_vtc_database()

    # 3. compute free VTCs
    free_vtcs = [vtc for vtc in full_db if vtc["id"] not in busy_list]

    return {
        "busy": busy_list,
        "free": free_vtcs,
        "total_free": len(free_vtcs)
    }
