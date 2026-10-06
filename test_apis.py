"""
Tests every external API the bot uses, from THIS machine.
Run:   python test_apis.py
Docker: docker compose run --rm action-server python test_apis.py   (after rebuilding)
"""
import os, time, requests
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

CONTACT = os.environ.get("CONTACT_EMAIL", "").strip() or "student-project@users.noreply.github.com"
H = {"User-Agent": f"EcoTravelAdvisor/1.0 (university coursework; contact: {CONTACT})"}
Q = '[out:json][timeout:10];node["railway"="station"](around:800,38.7223,-9.1393);out body 1;'

def check(name, fn):
    t = time.time()
    try:
        r = fn()
        ms = int((time.time() - t) * 1000)
        if r.status_code >= 400:
            print(f"FAIL  {name:28s} HTTP {r.status_code}  {r.text[:90]!r}  ({ms} ms)")
        else:
            print(f"OK    {name:28s} HTTP {r.status_code}  ({ms} ms)")
    except Exception as e:
        print(f"FAIL  {name:28s} {type(e).__name__}: {str(e)[:110]}")

print("Testing APIs from this machine...\n")
check("Open-Meteo geocoding", lambda: requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": "Lisbon", "count": 1}, headers=H, timeout=10))
check("Open-Meteo weather", lambda: requests.get("https://api.open-meteo.com/v1/forecast", params={"latitude": 38.7, "longitude": -9.1, "current": "temperature_2m"}, headers=H, timeout=10))
for url in ["https://overpass-api.de/api/interpreter", "https://overpass.openstreetmap.fr/api/interpreter", "https://overpass.private.coffee/api/interpreter"]:
    check("Overpass " + url.split("//")[1].split("/")[0], lambda u=url: requests.post(u, data={"data": Q}, headers=H, timeout=25))
check("Photon hotel search", lambda: requests.get("https://photon.komoot.io/api/", params={"q": "hotel", "lat": 38.72, "lon": -9.14, "limit": 3, "osm_tag": "tourism:hotel"}, headers=H, timeout=10))
check("Wikipedia", lambda: requests.get("https://en.wikipedia.org/api/rest_v1/page/summary/Lisbon", headers=H, timeout=10))
check("Frankfurter (currency)", lambda: requests.get("https://api.frankfurter.dev/v1/latest", params={"base": "EUR", "symbols": "GBP"}, headers=H, timeout=10))
key = os.environ.get("CLIMATIQ_API_KEY", "")
if key:
    check("Climatiq", lambda: requests.post("https://api.climatiq.io/data/v1/estimate", json={"emission_factor": {"activity_id": "passenger_train-route_type_na-fuel_source_na", "data_version": "^0"}, "parameters": {"distance": 800, "distance_unit": "km"}}, headers={"Authorization": f"Bearer {key}"}, timeout=15))
else:
    print("SKIP  Climatiq                      (no CLIMATIQ_API_KEY set)")

print("""
How to read this:
  All FAIL with ConnectionError / getaddrinfo  -> no internet, VPN, firewall or antivirus is blocking Python
  Only Overpass FAIL with HTTP 429/504         -> server busy, wait a minute and retry
  Overpass FAIL with HTTP 403/406              -> put your real name/email in the User-Agent
  All OK here but the bot still fails          -> the action server is running OLD code (restart it / rebuild Docker)
""")