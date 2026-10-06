"""
actions.py
----------
Custom action server for the Eco-Travel Advisor Rasa bot.

External APIs used (all free):
  - Climatiq            carbon estimates   (optional key, fallback table if absent)
  - Open-Meteo          geocoding + weather (no key)
  - Overpass / OSM      public transport + attractions (no key)
  - Wikipedia REST      place descriptions (no key)
  - Frankfurter (ECB)   currency conversion (no key)
"""

import os
import re
import json
import logging
from typing import Any, Text, Dict, List, Optional

import requests
from rasa_sdk import Action, Tracker, FormValidationAction
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet

logger = logging.getLogger(__name__)

# IMPORTANT — concurrency:
# rasa-sdk's action server runs as a SINGLE worker by default. Several
# actions here (validate_destination, find_hotels, etc.) chain multiple
# sequential blocking HTTP calls — while one of those is running, the
# action server cannot respond to ANY other incoming request (a button
# click, a plain greeting) until it finishes. This is what caused the
# "rest.message.received.timeout" errors and the "India sometimes doesn't
# respond" symptom: free public APIs are sometimes slow, and a slow one
# blocks everything else in the meantime.
# Fix: set ACTION_SERVER_SANIC_WORKERS=2 (or more) as an environment
# variable before starting the action server — this is a real, documented
# Rasa SDK setting, not something added here in code. Locally:
#   set ACTION_SERVER_SANIC_WORKERS=2   (Windows cmd)
#   $env:ACTION_SERVER_SANIC_WORKERS=2  (PowerShell)
#   export ACTION_SERVER_SANIC_WORKERS=2  (Mac/Linux)
# then run: rasa run actions
# In Docker, it's set directly in docker-compose.yml (see that file).

# Local runs (no Docker): read .env automatically if python-dotenv is installed.
# In Docker the variables are injected by docker-compose (env_file), so this is a no-op.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CLIMATIQ_API_KEY = os.environ.get("CLIMATIQ_API_KEY", "")
CLIMATIQ_URL = "https://api.climatiq.io/data/v1/estimate"

# Overpass rejects the placeholder "example.com" User-Agent with HTTP 406.
# Put your real email in .env as CONTACT_EMAIL=you@example.org
CONTACT = os.environ.get("CONTACT_EMAIL", "").strip() or "student-project@users.noreply.github.com"
HEADERS = {"User-Agent": f"EcoTravelAdvisor/1.0 (university coursework; contact: {CONTACT})"}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]
PHOTON_URL = "https://photon.komoot.io/api/"
WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
FRANKFURTER_URL = "https://api.frankfurter.dev/v1"

VISITABLE_HISTORIC = "castle|monument|ruins|fort|archaeological_site|city_gate"

_HERE = os.path.dirname(__file__)
MOCK_HOTELS_CANDIDATES = [
    os.path.join(_HERE, "..", "data", "eco_hotels.json"),
    os.path.join(_HERE, "data", "eco_hotels.json"),
    os.path.join(_HERE, "eco_hotels.json"),
]

FALLBACK_EMISSIONS_KG_PER_KM = {
    "plane": 0.246,
    "car": 0.171,
    "train": 0.041,
    "bus": 0.105,
}

DEFAULT_TRIP_DISTANCE_KM = 800


# --------------------------------------------------------------------------
# API helper functions (data layer, no Rasa code in here)
# --------------------------------------------------------------------------
_GEOCODE_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


class GeocodeError(Exception):
    """Raised when the geocoding service itself is unreachable (not 'place not found')."""


def geocode(name: str) -> Optional[Dict[str, Any]]:
    """Destination name -> {name, country, lat, lon} using Open-Meteo geocoding."""
    key = (name or "").strip().lower()
    if not key:
        return None
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    result = None
    try:
        resp = requests.get(
            GEOCODE_URL,
            params={"name": name, "count": 1, "language": "en", "format": "json"},
            headers=HEADERS, timeout=4,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if results:
            r = results[0]
            result = {
                "name": r.get("name", name),
                "country": r.get("country", ""),
                "lat": r["latitude"],
                "lon": r["longitude"],
                "feature_code": r.get("feature_code", ""),
            }
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Geocoding failed for %s: %s", name, e)
        raise GeocodeError(str(e)) from e
    _GEOCODE_CACHE[key] = result
    return result



_STOP_PLACES = {"there", "here", "it", "that", "this", "city", "town", "area", "my destination",
                "destination", "my trip", "trip", "the city", "that city", "this city", "nearby"}
_TRAILING = re.compile(r"\s+(today|tomorrow|tonight|now|please|right now|this week|this weekend|next week|next month)$", re.I)


def _place_candidates(text: str) -> List[str]:
    """Pull a place name out of 'what can I visit in Delhi' style questions."""
    for m in re.finditer(r"\b(?:in|at|near|around)\s+([^?.!,;]+)", text or "", re.I):
        tail = _TRAILING.sub("", m.group(1).strip()).strip(" '\"")
        tail = re.sub(r"^(the|a|an)\s+", "", tail, flags=re.I)
        if not tail or tail.lower() in _STOP_PLACES:
            continue
        words = tail.split()[:3]
        cands = []
        for n in range(len(words), 0, -1):
            c = " ".join(words[:n])
            if c.lower() not in _STOP_PLACES:
                cands.append(c)
        if cands:
            return cands
    return []


def _pick_destination(tracker: Tracker) -> Optional[str]:
    """A city named in the user's question wins; otherwise use the saved trip destination."""
    entity = next(tracker.get_latest_entity_values("destination"), None)
    text = (tracker.latest_message or {}).get("text", "")
    for candidate in ([entity] if entity else []) + _place_candidates(text):
        try:
            if geocode(candidate):
                return candidate
        except GeocodeError:
            break
    return tracker.get_slot("destination")


BROAD_FEATURE_CODES = ("CONT", "PCL", "ADM1", "TERR")


def _is_broad(place: Dict[str, Any]) -> bool:
    """True for continents, countries and big regions: no single centre to search around."""
    return str(place.get("feature_code", "")).startswith(BROAD_FEATURE_CODES)


def _resolve_place(dispatcher: CollectingDispatcher, destination: str) -> Optional[Dict[str, Any]]:
    """Geocode a destination; tell the user and return None if it cannot be resolved."""
    try:
        place = geocode(destination)
    except GeocodeError:
        dispatcher.utter_message(text="The location service isn't responding right now. Please try again in a moment.")
        return None
    if not place:
        dispatcher.utter_message(text=f"I couldn't find '{destination}' on the map. Please check the spelling.")
        return None
    return place



def _destination_intro(dispatcher: CollectingDispatcher, place: Dict[str, Any]) -> None:
    """Short intro to the destination: Wikipedia summary + current weather. Skips silently on failure."""
    lines = [f"📍 {place['name']}, {place['country']}".rstrip(", ")]
    link = None
    info = describe_place(place["name"])
    if info and info["summary"]:
        lines.append(info["summary"])
        link = info.get("url")
    try:
        cur = get_weather(place["lat"], place["lon"])["current"]
        lines.append(f"Right now: {cur['temperature_2m']}°C, {cur['precipitation']} mm rain, wind {cur['wind_speed_10m']} km/h.")
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Intro weather failed: %s", e)
    if len(lines) == 1:
        return
    if link:
        lines.append(f"(Source: Wikipedia, CC BY-SA - {link}. Weather: Open-Meteo.)")
    lines.append("You can ask me about the weather, public transport or attractions here at any time.")
    dispatcher.utter_message(text="\n".join(lines))


def get_weather(lat: float, lon: float) -> Dict[str, Any]:
    resp = requests.get(
        WEATHER_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,precipitation,wind_speed_10m",
            "daily": "temperature_2m_max,precipitation_sum",
            "forecast_days": 3,
            "timezone": "auto",
        },
        headers=HEADERS, timeout=5,
    )
    resp.raise_for_status()
    return resp.json()


def travel_advice(temp_c: float, rain_mm: float) -> str:
    if rain_mm > 5:
        return "wet - plan indoor activities, and public transport over cycling"
    if temp_c < 5:
        return "cold - walking tours will be hard going"
    if temp_c > 30:
        return "hot - avoid cycling and long walks in the middle of the day"
    return "good conditions for walking and cycling"


_OVERPASS_CACHE: Dict[str, List[Dict[str, Any]]] = {}


def _overpass(query: str) -> List[Dict[str, Any]]:
    """Run an Overpass query, trying several public mirrors (the main one is often busy)."""
    if query in _OVERPASS_CACHE:
        return _OVERPASS_CACHE[query]
    last_error: Exception = RuntimeError("no Overpass server tried")
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=6)
            if resp.status_code in (429, 502, 503, 504):
                logger.warning("Overpass %s returned HTTP %s, trying next server", url, resp.status_code)
                last_error = requests.HTTPError(f"HTTP {resp.status_code} from {url}")
                continue
            resp.raise_for_status()
            elements = resp.json().get("elements", [])
            _OVERPASS_CACHE[query] = elements
            return elements
        except (requests.RequestException, ValueError) as e:
            logger.warning("Overpass %s failed: %s", url, e)
            last_error = e
    raise last_error


def find_transport(lat: float, lon: float, radius_m: int = 1500, limit: int = 25) -> List[Dict[str, Any]]:
    query = f"""
    [out:json][timeout:8];
    (
      node["railway"="station"](around:{radius_m},{lat},{lon});
      node["railway"="subway_entrance"](around:{radius_m},{lat},{lon});
      node["railway"="tram_stop"](around:{radius_m},{lat},{lon});
    );
    out body {limit};
    """
    stops = []
    for el in _overpass(query):
        tags = el.get("tags", {})
        stops.append({
            "name": tags.get("name", "(unnamed)"),
            "type": tags.get("railway", "unknown"),
            "lat": el.get("lat"),
            "lon": el.get("lon"),
        })
    return stops


def find_attractions(lat: float, lon: float, radius_m: int = 1500, limit: int = 12) -> List[Dict[str, Any]]:
    query = f"""
    [out:json][timeout:8];
    (
      nwr["tourism"="museum"](around:{radius_m},{lat},{lon});
      nwr["historic"~"^({VISITABLE_HISTORIC})$"](around:{radius_m},{lat},{lon});
    );
    out center {limit};
    """
    sites = []
    for el in _overpass(query):
        tags = el.get("tags", {})
        if not tags.get("name"):
            continue
        sites.append({
            "name": tags["name"],
            "kind": tags.get("tourism") or tags.get("historic"),
            "wikipedia": tags.get("wikipedia"),   # e.g. "en:Belém Tower"
        })
    return sites


def describe_place(title: str, sentences: int = 2) -> Optional[Dict[str, Any]]:
    """Short Wikipedia summary, or None if there is no article."""
    import urllib.parse
    try:
        safe = urllib.parse.quote(title.replace(" ", "_"))
        resp = requests.get(WIKI_SUMMARY_URL + safe, headers=HEADERS, timeout=4)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        parts = data.get("extract", "").split(". ")
        short = ". ".join(parts[:sentences])
        if short and not short.endswith("."):
            short += "."
        return {
            "title": data.get("title"),
            "summary": short,
            "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
        }
    except (requests.RequestException, ValueError) as e:
        logger.warning("Wikipedia lookup failed for %s: %s", title, e)
        return None


def convert_currency(amount: float, from_cur: str, to_cur: str):
    """Returns (converted, rate, rate_date). Raises on failure."""
    resp = requests.get(
        f"{FRANKFURTER_URL}/latest",
        params={"base": from_cur, "symbols": to_cur},
        headers=HEADERS, timeout=5,
    )
    resp.raise_for_status()
    data = resp.json()
    rate = data["rates"][to_cur]
    return amount * rate, rate, data["date"]


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


STAR_PRICE_EUR = {1: 45, 2: 65, 3: 90, 4: 140, 5: 220}
DEFAULT_PRICE_EUR = 80


def find_hotels(lat: float, lon: float, radius_m: int = 3000, limit: int = 40) -> List[Dict[str, Any]]:
    """
    Real accommodation from OpenStreetMap (no key needed).
    OSM has no prices or carbon data, so:
      - price is an ESTIMATE from the star rating (default EUR 80)
      - carbon rating is derived from distance to the nearest rail/metro/tram stop
    """
    query = f"""
    [out:json][timeout:8];
    nwr["tourism"~"^(hotel|hostel|guest_house|apartment)$"]["name"](around:{radius_m},{lat},{lon});
    out center {limit};
    """
    elements = _overpass(query)
    try:
        stations = [s for s in find_transport(lat, lon, radius_m + 500, limit=150)
                    if s.get("lat") is not None]
    except Exception as e:  # transport data is a bonus; hotels still work without it
        logger.warning("Station lookup for hotel rating failed: %r", e)
        stations = []

    hotels, seen = [], set()
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        h_lat = el.get("lat") or el.get("center", {}).get("lat")
        h_lon = el.get("lon") or el.get("center", {}).get("lon")
        if not name or name.lower() in seen or h_lat is None:
            continue
        seen.add(name.lower())

        try:
            stars = int(float(str(tags.get("stars", "")).split()[0]))
        except (ValueError, IndexError):
            stars = None

        if stations:
            dist = min(_haversine_m(h_lat, h_lon, s["lat"], s["lon"]) for s in stations)
            rating = "low" if dist <= 600 else ("medium" if dist <= 1500 else "high")
            note = f"{int(round(dist, -1))} m from nearest station"
        else:
            rating, note = "medium", "station distance unknown"

        hotels.append({
            "name": name[:60],
            "price_per_night": STAR_PRICE_EUR.get(stars, DEFAULT_PRICE_EUR),
            "price_estimated": True,
            "carbon_rating": rating,
            "eco_certification": ("Green Key" if tags.get("green_key") and tags.get("green_key") != "no"
                                  else (tags.get("ecolabel") or "none")),
            "note": note,
        })
    return hotels


def find_hotels_photon(lat: float, lon: float, radius_m: int = 8000, limit: int = 40) -> List[Dict[str, Any]]:
    """Second live source (OpenStreetMap data via Photon). No key. Used if Overpass fails."""
    resp = requests.get(
        PHOTON_URL,
        params={"q": "hotel", "lat": lat, "lon": lon, "limit": limit, "osm_tag": "tourism:hotel"},
        headers=HEADERS, timeout=6,
    )
    resp.raise_for_status()
    hotels, seen = [], set()
    for f in resp.json().get("features", []):
        name = ((f.get("properties") or {}).get("name") or "").strip()
        try:
            h_lon, h_lat = f["geometry"]["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            continue
        if not name or name.lower() in seen or _haversine_m(lat, lon, h_lat, h_lon) > radius_m:
            continue
        seen.add(name.lower())
        hotels.append({
            "name": name[:60],
            "price_per_night": DEFAULT_PRICE_EUR,
            "price_estimated": True,
            "carbon_rating": "medium",
            "eco_certification": "none",
            "note": "transport access unknown",
        })
    return hotels


def _load_mock_hotels(destination: str) -> List[Dict[str, Any]]:
    for path in MOCK_HOTELS_CANDIDATES:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                key = (destination or "default").strip().lower()
                return data.get(key, data.get("default", []))
            except json.JSONDecodeError as e:
                logger.error("eco_hotels.json at %s is not valid JSON: %s", path, e)
                return []
    logger.error("eco_hotels.json not found in any of: %s", MOCK_HOTELS_CANDIDATES)
    return []


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
class ActionCalculateCarbonFootprint(Action):
    def name(self) -> Text:
        return "action_calculate_carbon_footprint"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        distance_km = DEFAULT_TRIP_DISTANCE_KM
        results = {}
        api_modes = 0

        for mode in ["plane", "car", "train", "bus"]:
            co2_kg, from_api = self._get_emissions(mode, distance_km)
            results[mode] = round(co2_kg, 1)
            api_modes += 1 if from_api else 0

        greenest_mode = min(results, key=results.get)
        message_lines = [f"Here's the estimated carbon footprint for a {distance_km} km trip:"]
        for mode, co2 in sorted(results.items(), key=lambda x: x[1]):
            colour = "🟢" if co2 == results[greenest_mode] else (
                "🟡" if co2 < results["plane"] else "🔴"
            )
            message_lines.append(f"{colour} {mode.capitalize()}: {co2} kg CO2e")
        message_lines.append(f"\nLowest-carbon option: **{greenest_mode}**")
        if api_modes == 4:
            message_lines.append("(Source: Climatiq API)")

        dispatcher.utter_message(text="\n".join(message_lines))
        return [SlotSet("carbon_score", json.dumps(results))]

    def _get_emissions(self, mode: Text, distance_km: float):
        """Returns (kg CO2e, True if it came from the Climatiq API)."""
        fallback = FALLBACK_EMISSIONS_KG_PER_KM[mode] * distance_km
        if not CLIMATIQ_API_KEY:
            return fallback, False

        activity_map = {
            "plane": "passenger_flight-route_type_domestic-aircraft_type_na",
            "car": "passenger_vehicle-vehicle_type_car-fuel_source_na",
            "train": "passenger_train-route_type_na-fuel_source_na",
            "bus": "passenger_vehicle-vehicle_type_bus-fuel_source_na",
        }
        payload = {
            "emission_factor": {"activity_id": activity_map[mode], "data_version": "^0"},
            "parameters": {"distance": distance_km, "distance_unit": "km"},
        }
        headers = {"Authorization": f"Bearer {CLIMATIQ_API_KEY}"}
        try:
            resp = requests.post(CLIMATIQ_URL, json=payload, headers=headers, timeout=4)
            if resp.status_code >= 400:
                logger.warning("Climatiq HTTP %s for mode=%s: %s", resp.status_code, mode, resp.text[:300])
                return fallback, False
            return float(resp.json()["co2e"]), True
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning("Climatiq call failed for mode=%s: %s. Using fallback.", mode, e)
            return fallback, False


class ActionRecommendOptions(Action):
    def name(self) -> Text:
        return "action_recommend_options"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        destination = tracker.get_slot("destination") or "your destination"
        sustainability_level = tracker.get_slot("sustainability_level") or "medium"

        hotels, live = self._get_hotels(destination)
        if not hotels:
            dispatcher.utter_message(
                text=f"Sorry, I couldn't find accommodation options for {destination} right now. "
                     f"Would you like me to connect you with a human advisor?"
            )
            return []

        weights = {
            "low": {"carbon": 0.2, "price": 0.7, "cert": 0.1},
            "medium": {"carbon": 0.4, "price": 0.4, "cert": 0.2},
            "high": {"carbon": 0.6, "price": 0.2, "cert": 0.2},
        }[sustainability_level]

        carbon_rank = {"low": 1.0, "medium": 0.6, "high": 0.2}
        max_price = max(h["price_per_night"] for h in hotels) or 1

        scored = []
        for h in hotels:
            price_score = 1 - (h["price_per_night"] / max_price)
            carbon_score = carbon_rank.get(h["carbon_rating"], 0.5)
            cert_score = 1.0 if h.get("eco_certification", "none") != "none" else 0.0
            total = (weights["carbon"] * carbon_score
                     + weights["price"] * price_score
                     + weights["cert"] * cert_score)
            scored.append((total, h))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_hotels = [h for _, h in scored[:5]]

        if live:
            lines = [f"Top stays in {destination.title()} for you, ranked by your sustainability preference:"]
        else:
            lines = [f"Top eco-friendly stays in {destination.title()} for you:"]
        for h in top_hotels:
            flag = "🟢" if h["carbon_rating"] == "low" else ("🟡" if h["carbon_rating"] == "medium" else "🔴")
            cert = h.get("eco_certification", "none")
            price = f"~€{h['price_per_night']}/night (est.)" if h.get("price_estimated") else f"€{h['price_per_night']}/night"
            extra = h.get("note") or cert
            if h.get("note") and cert != "none":
                extra += f" · certified: {cert}"
            lines.append(f"{flag} **{h['name']}** — {price} — {extra}")

        if live:
            lines.append("\n(Hotels: © OpenStreetMap contributors. Prices are estimates. OpenStreetMap has no reliable eco-certification data, so the colour is only a proxy: how close the hotel is to public transport. A hotel is shown as certified only if its map entry says so.)")
        buttons = [{"title": h["name"], "payload": h["name"]} for h in top_hotels]
        dispatcher.utter_message(text="\n".join(lines), buttons=buttons)
        return [SlotSet("offered_hotels", [h["name"] for h in top_hotels])]


    def _get_hotels(self, destination: str):
        """Live OpenStreetMap hotels first; local eco_hotels.json if the API fails or finds nothing."""
        try:
            place = geocode(destination)
        except GeocodeError:
            place = None
        if place and not _is_broad(place):
            for source in (find_hotels, find_hotels_photon):
                try:
                    hotels = source(place["lat"], place["lon"])
                    if hotels:
                        return hotels, True
                except Exception as e:
                    logger.warning("Live hotel source %s failed: %r", source.__name__, e)
        return _load_mock_hotels(destination), False


class ActionGetWeather(Action):
    def name(self) -> Text:
        return "action_get_weather"

    def run(self, dispatcher, tracker, domain):
        destination = _pick_destination(tracker)
        if not destination:
            dispatcher.utter_message(text="Tell me your destination first and I'll check the weather there.")
            return []

        place = _resolve_place(dispatcher, destination)
        if not place:
            return []

        try:
            data = get_weather(place["lat"], place["lon"])
        except (requests.RequestException, ValueError) as e:
            logger.warning("Weather API failed: %s", e)
            dispatcher.utter_message(text="The weather service isn't responding right now. Please try again in a moment.")
            return []

        cur = data["current"]
        daily = data["daily"]
        lines = [
            f"Weather in {place['name']}, {place['country']} right now: "
            f"{cur['temperature_2m']}°C, {cur['precipitation']} mm rain, wind {cur['wind_speed_10m']} km/h.",
            "Next 3 days:",
        ]
        for i, date in enumerate(daily["time"]):
            tmax = daily["temperature_2m_max"][i]
            rain = daily["precipitation_sum"][i]
            lines.append(f"• {date}: max {tmax}°C, rain {rain} mm - {travel_advice(tmax, rain)}")
        lines.append("(Forecast data: Open-Meteo. This covers the next 3 days, not your travel dates.)")
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionFindTransport(Action):
    def name(self) -> Text:
        return "action_find_transport"

    def run(self, dispatcher, tracker, domain):
        destination = _pick_destination(tracker)
        if not destination:
            dispatcher.utter_message(text="Tell me your destination first and I'll check its public transport.")
            return []

        place = _resolve_place(dispatcher, destination)
        if not place:
            return []
        if _is_broad(place):
            dispatcher.utter_message(text=f"{place['name']} is a whole country or region, so there is no single centre to search around. Tell me a specific city and I can check it, for example the city you plan to stay in.")
            return []

        try:
            stops = find_transport(place["lat"], place["lon"])
        except (requests.RequestException, ValueError) as e:
            logger.warning("Overpass transport query failed on all servers: %r", e)
            dispatcher.utter_message(text="The map service is busy right now. Please try again in a moment.")
            return []

        n = len(stops)
        if n >= 10:
            verdict = "excellent - you will not need a car"
        elif n >= 3:
            verdict = "reasonable, but check routes for your specific trip"
        else:
            verdict = "limited - factor transport emissions into your planning"

        counts: Dict[str, int] = {}
        for s in stops:
            counts[s["type"]] = counts.get(s["type"], 0) + 1
        label = {"station": "rail stations", "subway_entrance": "metro entrances", "tram_stop": "tram stops"}
        breakdown = ", ".join(f"{c} {label.get(k, k)}" for k, c in counts.items()) or "none found"

        names = list(dict.fromkeys(s["name"] for s in stops if s["name"] != "(unnamed)"))[:3]
        lines = [
            f"Public transport near the centre of {place['name']}: {verdict}.",
            f"Within 1.5 km: {breakdown}.",
        ]
        if names:
            lines.append("Examples: " + ", ".join(names) + ".")
        lines.append("(Map data © OpenStreetMap contributors. I can show where stops are, not live timetables.)")
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionFindAttractions(Action):
    def name(self) -> Text:
        return "action_find_attractions"

    def run(self, dispatcher, tracker, domain):
        destination = _pick_destination(tracker)
        if not destination:
            dispatcher.utter_message(text="Tell me your destination first and I'll look for cultural sites.")
            return []

        place = _resolve_place(dispatcher, destination)
        if not place:
            return []
        if _is_broad(place):
            dispatcher.utter_message(text=f"{place['name']} is a whole country or region, so there is no single centre to search around. Tell me a specific city and I can check it, for example the city you plan to stay in.")
            return []

        try:
            sites = find_attractions(place["lat"], place["lon"])
        except (requests.RequestException, ValueError) as e:
            logger.warning("Overpass attractions query failed on all servers: %r", e)
            dispatcher.utter_message(text="The map service is busy right now. Please try again in a moment.")
            return []

        if not sites:
            dispatcher.utter_message(text=f"I couldn't find museums or historic sites near the centre of {place['name']}.")
            return []

        lines = [f"Cultural sites worth visiting in {place['name']}:"]
        sources = []
        for site in sites[:5]:
            kind = (site["kind"] or "site").replace("_", " ")
            line = f"• **{site['name']}** ({kind})"
            if len(sources) < 3:
                title = site["name"]
                tag = site.get("wikipedia")
                if tag and tag.startswith("en:"):
                    title = tag[3:]
                info = describe_place(title)
                if info and info["summary"]:
                    line += f" - {info['summary']}"
                    if info["url"]:
                        sources.append(info["url"])
            lines.append(line)

        lines.append("(Places: © OpenStreetMap contributors. Descriptions: Wikipedia, CC BY-SA.)")
        for url in sources:
            lines.append(url)
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionConvertBudget(Action):
    def name(self) -> Text:
        return "action_convert_budget"

    def run(self, dispatcher, tracker, domain):
        budget = tracker.get_slot("budget")
        target = next(tracker.get_latest_entity_values("currency"), None)

        if not budget:
            dispatcher.utter_message(text="Tell me your budget first (in EUR) and I can convert it.")
            return []
        if not target:
            dispatcher.utter_message(text="Which currency should I convert to? For example GBP, USD or JPY.")
            return []

        target = target.strip().upper()
        try:
            amount = float(str(budget).replace(",", ""))
            if target == "EUR":
                dispatcher.utter_message(text=f"Your budget is already in EUR: €{amount:,.2f}.")
                return []
            converted, rate, date = convert_currency(amount, "EUR", target)
        except ValueError:
            dispatcher.utter_message(text="I couldn't read your budget as a number.")
            return []
        except KeyError:
            dispatcher.utter_message(text=f"I don't recognise the currency code {target}.")
            return []
        except requests.RequestException as e:
            logger.warning("Frankfurter failed: %s", e)
            dispatcher.utter_message(text=f"I couldn't convert to {target}. Check the currency code or try again.")
            return []

        dispatcher.utter_message(
            text=f"Your budget of €{amount:,.2f} is about {converted:,.2f} {target} "
                 f"(ECB reference rate {rate} on {date}; real card or cash rates will differ slightly)."
        )
        return []


def _do_handover(dispatcher: CollectingDispatcher, tracker: Tracker) -> None:
    """Builds the handover package and tells the user. Does NOT pause the chat."""
    context = {
        "conversation_id": tracker.sender_id,
        "destination": tracker.get_slot("destination"),
        "travel_dates": tracker.get_slot("travel_dates"),
        "budget": tracker.get_slot("budget"),
        "sustainability_level": tracker.get_slot("sustainability_level"),
        "chosen_transport": tracker.get_slot("chosen_transport"),
        "carbon_score": tracker.get_slot("carbon_score"),
        "last_messages": [
            e.get("text") for e in tracker.events
            if e.get("event") == "user" and e.get("text")
        ][-5:],
    }
    logger.info("HUMAN HANDOVER PACKAGE: %s", json.dumps(context))
    dispatcher.utter_message(response="utter_handover_notice")
    dispatcher.utter_message(response="utter_handover_confirmation")


class ActionHumanHandover(Action):
    def name(self) -> Text:
        return "action_human_handover"

    def run(self, dispatcher, tracker, domain):
        _do_handover(dispatcher, tracker)
        return []


class ActionTwoStageFallback(Action):
    """1st unrecognised -> buttons; 2nd consecutive unrecognised -> human handover."""
    def name(self) -> Text:
        return "action_two_stage_fallback"

    def run(self, dispatcher, tracker, domain):
        user_events = [e for e in tracker.events if e.get("event") == "user"]
        prev_intent = None
        if len(user_events) >= 2:
            prev_intent = ((user_events[-2].get("parse_data") or {}).get("intent") or {}).get("name")

        if prev_intent == "nlu_fallback":
            dispatcher.utter_message(response="utter_default_fallback_stage2")
            _do_handover(dispatcher, tracker)
            return [SlotSet("fallback_count", 0)]

        dispatcher.utter_message(response="utter_default_fallback_stage1")
        return [SlotSet("fallback_count", 1)]


class ActionConfirmBooking(Action):
    def name(self) -> Text:
        return "action_confirm_booking"

    def run(self, dispatcher, tracker, domain):
        hotel = tracker.get_slot("selected_hotel") or "your chosen hotel"
        payment = (tracker.get_slot("payment_method") or "your selected method").replace("_", " ").title()
        destination = tracker.get_slot("destination") or "your destination"
        transport = tracker.get_slot("chosen_transport") or "your chosen transport"

        summary = (
            f"✅ Booking confirmed!\n"
            f"Destination: {destination.title()}\n"
            f"Transport: {transport.title()}\n"
            f"Hotel: {hotel}\n"
            f"Payment method: {payment}"
        )
        dispatcher.utter_message(text=summary)
        return []


class ValidateTripForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_trip_form"

    def validate_destination(self, slot_value, dispatcher, tracker, domain):
        greetings = ["hi", "hello", "hey", "hii", "hiii", "hiiii", "hy", "hyy", "yo", "hola", "halo", "hallo", "hai", "helo", "heyy"]
        if slot_value and slot_value.strip().lower() in greetings:
            return {"destination": None}   # just a greeting: the form simply asks again
        if not slot_value or slot_value.strip().startswith("/"):
            dispatcher.utter_message(text="That doesn't look like a destination — please type a real city or country name.")
            return {"destination": None}
        # verify the place exists (geocoding API) and introduce it to the user
        place = None
        try:
            place = geocode(slot_value.strip())
            if place is None:
                dispatcher.utter_message(text=f"I couldn't find '{slot_value.strip()}' on the map. Please check the spelling or try a nearby city.")
                return {"destination": None}
        except GeocodeError:
            pass  # service down: accept the value instead of trapping the user in the form
        if place:
            _destination_intro(dispatcher, place)
        return {"destination": slot_value.strip()}

    def validate_travel_dates(self, slot_value, dispatcher, tracker, domain):
        if not slot_value or slot_value.strip().startswith("/"):
            dispatcher.utter_message(text="Please tell me roughly when you're planning to travel.")
            return {"travel_dates": None}
        return {"travel_dates": slot_value.strip()}

    def validate_budget(self, slot_value, dispatcher, tracker, domain):
        cleaned = str(slot_value).replace(",", "").replace("€", "").strip()
        try:
            float(cleaned)
            return {"budget": cleaned}
        except ValueError:
            dispatcher.utter_message(text="Please enter your budget as just a number, e.g. 1500")
            return {"budget": None}

    def validate_sustainability_level(self, slot_value, dispatcher, tracker, domain):
        value = str(slot_value).strip().lower()
        if value in ("low", "medium", "high"):
            return {"sustainability_level": value}
        dispatcher.utter_message(text="Please choose low, medium or high.")
        return {"sustainability_level": None}

    def validate_chosen_transport(self, slot_value, dispatcher, tracker, domain):
        value = str(slot_value).strip().lower()
        if value in ("train", "bus", "car", "plane"):
            return {"chosen_transport": value}
        dispatcher.utter_message(text="Please choose train, bus, car or plane.")
        return {"chosen_transport": None}


class ValidateBookingForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_booking_form"

    def validate_selected_hotel(self, slot_value, dispatcher, tracker, domain):
        offered = tracker.get_slot("offered_hotels") or []
        if not slot_value or slot_value.strip().startswith("/"):
            dispatcher.utter_message(text="Please tap or type one of the hotel names listed above.")
            return {"selected_hotel": None}
        match = next((h for h in offered if h.lower() == slot_value.strip().lower()), None)
        if offered and not match:
            dispatcher.utter_message(text=f"I didn't recognize that hotel. Please choose one of: {', '.join(offered)}")
            return {"selected_hotel": None}
        return {"selected_hotel": match or slot_value.strip()}