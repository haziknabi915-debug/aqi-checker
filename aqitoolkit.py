so basically this project works on place to place it will measure the aqi level of air in differnet satates and in differnet countries ."""
================================================================================
AQI TOOLKIT
================================================================================
A full-featured command-line application for tracking Air Quality Index (AQI)
data, built entirely in plain Python (only external dependency: `requests`).

FEATURES
--------
  - Check live AQI for any city (WAQI API)
  - Local caching so repeated lookups don't hammer the API
  - SQLite-backed search history with statistics (avg / min / max / trend)
  - Compare AQI across multiple cities side-by-side
  - Personal watchlist with threshold alerts
  - Export history to CSV, JSON, or a plain-text report
  - Persistent config file (API token, default city, units, watchlist)
  - Both an interactive menu AND a scriptable command-line interface
  - A small built-in test suite for the pure logic (no network needed)

HOW TO RUN IT
-------------
  1. Install the one dependency:
         pip install requests

  2. Get a FREE API token (~30 seconds, no credit card):
         https://aqicn.org/data-platform/token/

  3. Set it once via the built-in config command:
         python aqi_toolkit.py config --set-token YOUR_TOKEN_HERE

  4. Run it interactively:
         python aqi_toolkit.py

     ...or use it as a scriptable CLI:
         python aqi_toolkit.py check london
         python aqi_toolkit.py compare london paris tokyo
         python aqi_toolkit.py watch add delhi
         python aqi_toolkit.py history --city london
         python aqi_toolkit.py export csv myreport.csv
         python aqi_toolkit.py stats london
         python aqi_toolkit.py --test        (runs the built-in test suite)

FILE STRUCTURE OF THIS SCRIPT (use Ctrl+F to jump to a section)
-----------------------------------------------------------------
  SECTION 1  - Constants & configuration defaults
  SECTION 2  - Logging setup
  SECTION 3  - Custom exceptions
  SECTION 4  - Data models (AQIReading)
  SECTION 5  - Config manager
  SECTION 6  - Local cache manager
  SECTION 7  - AQI classification (EPA breakpoints)
  SECTION 8  - API client
  SECTION 9  - History database (SQLite)
  SECTION 10 - Statistics helpers
  SECTION 11 - Exporters (CSV / JSON / text report)
  SECTION 12 - City comparison
  SECTION 13 - Watchlist manager
  SECTION 14 - Console formatting helpers
  SECTION 15 - Interactive menu
  SECTION 16 - Command-line interface (argparse)
  SECTION 17 - Built-in test suite
  SECTION 18 - Entry point
================================================================================
"""

import argparse
import csv
import json
import logging
import os
import sqlite3
import statistics
import sys
import time
import unittest
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

import requests


# ==============================================================================
# SECTION 1: CONSTANTS & CONFIGURATION DEFAULTS
# ==============================================================================

APP_NAME = "AQI Toolkit"
APP_VERSION = "1.0.0"

# Everything the app saves (config, cache, history, logs) lives in a hidden
# folder in the user's home directory, so it works the same on every OS.
APP_DIR = Path.home() / ".aqi_toolkit"
CONFIG_FILE = APP_DIR / "config.json"
CACHE_FILE = APP_DIR / "cache.json"
HISTORY_DB = APP_DIR / "history.db"
LOG_FILE = APP_DIR / "aqi_toolkit.log"

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_token": "demo",
    "default_city": "beijing",
    "cache_ttl_minutes": 10,
    "temperature_unit": "C",
    "watchlist": [],
    "alert_threshold": 150,
}

AQI_API_URL = "https://api.waqi.info/feed/{city}/?token={token}"

# US EPA AQI breakpoints, as (low, high, category, color_name, health_message)
AQI_BREAKPOINTS: List[Tuple[int, int, str, str, str]] = [
    (0, 50, "Good", "green",
     "Air quality is satisfactory, and air pollution poses little or no risk."),
    (51, 100, "Moderate", "yellow",
     "Air quality is acceptable. There may be a risk for people unusually "
     "sensitive to air pollution."),
    (101, 150, "Unhealthy for Sensitive Groups", "orange",
     "Members of sensitive groups may experience health effects. The "
     "general public is less likely to be affected."),
    (151, 200, "Unhealthy", "red",
     "Some members of the general public may experience health effects; "
     "sensitive groups may experience more serious effects."),
    (201, 300, "Very Unhealthy", "purple",
     "Health alert: The risk of health effects is increased for everyone."),
    (301, 500, "Hazardous", "maroon",
     "Health warning of emergency conditions: everyone is more likely to "
     "be affected."),
]


# ==============================================================================
# SECTION 2: LOGGING SETUP
# ==============================================================================

def setup_logging() -> logging.Logger:
    """
    Configure a logger that writes to a rotating-ish log file (simple
    append-only file here) as well as the console for warnings and errors.
    """
    APP_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("aqi_toolkit")
    logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers if setup_logging() is called twice.
    if logger.handlers:
        return logger

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_format)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_format = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler.setFormatter(console_format)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


log = setup_logging()


# ==============================================================================
# SECTION 3: CUSTOM EXCEPTIONS
# ==============================================================================

class AQIToolkitError(Exception):
    """Base class for every error this app raises on purpose."""


class APIError(AQIToolkitError):
    """Raised when the AQI API can't be reached or returns bad data."""


class CityNotFoundError(AQIToolkitError):
    """Raised when a city name doesn't match any known monitoring station."""


class ConfigError(AQIToolkitError):
    """Raised when the config file is missing or corrupted."""


# ==============================================================================
# SECTION 4: DATA MODELS
# ==============================================================================

@dataclass
class AQIReading:
    """
    A single AQI reading for one city at one point in time.
    Using a dataclass instead of a plain dict gives us type hints,
    a free __repr__, and easy conversion to/from dictionaries.
    """

    city: str
    aqi: Optional[int]
    dominant_pollutant: str
    pm25: Any
    pm10: Any
    temperature: Any
    humidity: Any
    station_time: str
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def category(self) -> str:
        return classify_aqi(self.aqi)[0]

    @property
    def color(self) -> str:
        return classify_aqi(self.aqi)[1]

    @property
    def health_message(self) -> str:
        return classify_aqi(self.aqi)[2]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category
        return d

    def to_row(self) -> List[Any]:
        """Flat representation used for table printing and CSV export."""
        return [
            self.fetched_at,
            self.city,
            self.aqi,
            self.category,
            self.dominant_pollutant,
            self.pm25,
            self.pm10,
            self.temperature,
            self.humidity,
        ]

    ROW_HEADERS = [
        "Fetched At", "City", "AQI", "Category",
        "Dominant Pollutant", "PM2.5", "PM10", "Temp", "Humidity",
    ]


# ==============================================================================
# SECTION 5: CONFIG MANAGER
# ==============================================================================

class ConfigManager:
    """
    Loads, saves, and provides typed access to the app's persistent
    settings (API token, default city, watchlist, etc).
    """

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.data: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            self.data = dict(DEFAULT_CONFIG)
            self.save()
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Config file corrupted (%s), resetting to defaults.", e)
            loaded = {}

        # Merge with defaults so new config keys added in future versions
        # of this script don't break older config files.
        self.data = dict(DEFAULT_CONFIG)
        self.data.update(loaded)
        self.save()

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except OSError as e:
            raise ConfigError(f"Could not write config file: {e}") from e

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.save()

    # ---- convenience accessors -------------------------------------------
    @property
    def api_token(self) -> str:
        return self.data.get("api_token", "demo")

    @property
    def default_city(self) -> str:
        return self.data.get("default_city", "beijing")

    @property
    def cache_ttl_minutes(self) -> int:
        return int(self.data.get("cache_ttl_minutes", 10))

    @property
    def watchlist(self) -> List[str]:
        return list(self.data.get("watchlist", []))

    @property
    def alert_threshold(self) -> int:
        return int(self.data.get("alert_threshold", 150))

    def add_to_watchlist(self, city: str) -> bool:
        wl = self.watchlist
        city = city.strip().lower()
        if city in wl:
            return False
        wl.append(city)
        self.set("watchlist", wl)
        return True

    def remove_from_watchlist(self, city: str) -> bool:
        wl = self.watchlist
        city = city.strip().lower()
        if city not in wl:
            return False
        wl.remove(city)
        self.set("watchlist", wl)
        return True


# ==============================================================================
# SECTION 6: LOCAL CACHE MANAGER
# ==============================================================================

class CacheManager:
    """
    A tiny JSON-file cache so repeated lookups of the same city within a
    short window don't need a fresh network request every time.
    """

    def __init__(self, path: Path = CACHE_FILE, ttl_minutes: int = 10):
        self.path = path
        self.ttl = timedelta(minutes=ttl_minutes)
        self._data: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError as e:
            log.warning("Could not write cache file: %s", e)

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._data.get(key.lower())
        if not entry:
            return None

        cached_at = datetime.fromisoformat(entry["cached_at"])
        if datetime.now() - cached_at > self.ttl:
            return None  # expired

        return entry["value"]

    def set(self, key: str, value: Dict[str, Any]) -> None:
        self._data[key.lower()] = {
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "value": value,
        }
        self._save()

    def clear(self) -> None:
        self._data = {}
        self._save()


# ==============================================================================
# SECTION 7: AQI CLASSIFICATION
# ==============================================================================

def classify_aqi(aqi: Optional[int]) -> Tuple[str, str, str]:
    """
    Given a numeric AQI value, return (category, color_name, health_message)
    using the standard US EPA breakpoint table.
    """
    if aqi is None:
        return ("Unknown", "gray", "No data available.")

    try:
        aqi = int(aqi)
    except (TypeError, ValueError):
        return ("Unknown", "gray", "No data available.")

    for low, high, category, color, message in AQI_BREAKPOINTS:
        if low <= aqi <= high:
            return (category, color, message)

    # Above the top of the table (over 500) is still "Hazardous".
    if aqi > AQI_BREAKPOINTS[-1][1]:
        return AQI_BREAKPOINTS[-1][2], AQI_BREAKPOINTS[-1][3], AQI_BREAKPOINTS[-1][4]

    return ("Unknown", "gray", "No data available.")


# ==============================================================================
# SECTION 8: API CLIENT
# ==============================================================================

class AQIClient:
    """Wraps calls to the WAQI API, with caching and basic retry logic."""

    def __init__(self, token: str, cache: Optional[CacheManager] = None,
                 max_retries: int = 2, timeout: int = 5):
        self.token = token
        self.cache = cache
        self.max_retries = max_retries
        self.timeout = timeout

    def fetch(self, city: str, use_cache: bool = True) -> AQIReading:
        """
        Fetch an AQIReading for a city. Raises CityNotFoundError or
        APIError on failure. Uses the cache first if one was provided.
        """
        city = city.strip().lower()
        if not city:
            raise CityNotFoundError("City name cannot be empty.")

        if use_cache and self.cache:
            cached = self.cache.get(city)
            if cached is not None:
                log.debug("Cache hit for %s", city)
                return AQIReading(**cached)

        raw = self._request_with_retries(city)
        reading = self._parse_response(city, raw)

        if self.cache:
            self.cache.set(city, asdict(reading))

        return reading

    def _request_with_retries(self, city: str) -> Dict[str, Any]:
        url = AQI_API_URL.format(city=city, token=self.token)
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                last_error = e
                log.warning("Attempt %d/%d failed for %s: %s",
                            attempt, self.max_retries, city, e)
                time.sleep(0.5 * attempt)  # small backoff between retries

        raise APIError(f"Could not reach the AQI API for '{city}': {last_error}")

    @staticmethod
    def _parse_response(city: str, data: Dict[str, Any]) -> AQIReading:
        if data.get("status") != "ok":
            raise CityNotFoundError(
                f"No AQI station found for '{city}'. Try a different name."
            )

        d = data.get("data", {})
        iaqi = d.get("iaqi", {})

        return AQIReading(
            city=d.get("city", {}).get("name", city),
            aqi=d.get("aqi"),
            dominant_pollutant=d.get("dominentpol", "n/a"),
            pm25=iaqi.get("pm25", {}).get("v", "n/a"),
            pm10=iaqi.get("pm10", {}).get("v", "n/a"),
            temperature=iaqi.get("t", {}).get("v", "n/a"),
            humidity=iaqi.get("h", {}).get("v", "n/a"),
            station_time=d.get("time", {}).get("s", "unknown"),
        )


# ==============================================================================
# SECTION 9: HISTORY DATABASE (SQLITE)
# ==============================================================================

class HistoryDatabase:
    """Stores every AQI lookup ever made, so we can compute stats over time."""

    def __init__(self, path: Path = HISTORY_DB):
        self.path = path
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city TEXT NOT NULL,
                aqi INTEGER,
                category TEXT,
                dominant_pollutant TEXT,
                pm25 TEXT,
                pm10 TEXT,
                temperature TEXT,
                humidity TEXT,
                station_time TEXT,
                fetched_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        conn.close()

    def add(self, reading: AQIReading) -> None:
        conn = self._connect()
        conn.execute(
            """
            INSERT INTO readings
                (city, aqi, category, dominant_pollutant, pm25, pm10,
                 temperature, humidity, station_time, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reading.city, reading.aqi, reading.category,
                reading.dominant_pollutant, str(reading.pm25), str(reading.pm10),
                str(reading.temperature), str(reading.humidity),
                reading.station_time, reading.fetched_at,
            ),
        )
        conn.commit()
        conn.close()

    def all_for_city(self, city: str) -> List[sqlite3.Row]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM readings WHERE LOWER(city) LIKE ? ORDER BY fetched_at DESC",
            (f"%{city.lower()}%",),
        ).fetchall()
        conn.close()
        return rows

    def all(self, limit: int = 100) -> List[sqlite3.Row]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM readings ORDER BY fetched_at DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return rows

    def distinct_cities(self) -> List[str]:
        conn = self._connect()
        rows = conn.execute("SELECT DISTINCT city FROM readings ORDER BY city").fetchall()
        conn.close()
        return [r["city"] for r in rows]

    def delete_older_than(self, days: int) -> int:
        """Housekeeping: delete history rows older than N days. Returns count removed."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self._connect()
        cursor = conn.execute("DELETE FROM readings WHERE fetched_at < ?", (cutoff,))
        conn.commit()
        removed = cursor.rowcount
        conn.close()
        return removed


# ==============================================================================
# SECTION 10: STATISTICS HELPERS
# ==============================================================================

def compute_city_stats(rows: List[sqlite3.Row]) -> Dict[str, Any]:
    """
    Given a list of history rows for one city, compute summary statistics.
    Rows with a non-numeric AQI are ignored.
    """
    values = []
    for r in rows:
        try:
            values.append(int(r["aqi"]))
        except (TypeError, ValueError):
            continue

    if not values:
        return {
            "count": 0, "average": None, "minimum": None,
            "maximum": None, "trend": "not enough data",
        }

    trend = "flat"
    if len(values) >= 2:
        # rows are ordered newest-first, so compare latest to a bit earlier
        newest = values[0]
        oldest = values[-1]
        if newest > oldest + 5:
            trend = "worsening"
        elif newest < oldest - 5:
            trend = "improving"

    return {
        "count": len(values),
        "average": round(statistics.mean(values), 1),
        "minimum": min(values),
        "maximum": max(values),
        "trend": trend,
    }


# ==============================================================================
# SECTION 11: EXPORTERS
# ==============================================================================

def export_to_csv(rows: List[sqlite3.Row], path: str) -> int:
    """Write history rows to a CSV file. Returns the number of rows written."""
    if not rows:
        return 0

    fieldnames = rows[0].keys()
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    return len(rows)


def export_to_json(rows: List[sqlite3.Row], path: str) -> int:
    """Write history rows to a JSON file. Returns the number of rows written."""
    data = [dict(r) for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return len(data)


def export_to_text_report(rows: List[sqlite3.Row], path: str) -> int:
    """Write a simple human-readable text report. Returns rows written."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{APP_NAME} - History Report\n")
        f.write(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n")
        f.write("=" * 60 + "\n\n")

        for r in rows:
            f.write(f"City:      {r['city']}\n")
            f.write(f"AQI:       {r['aqi']} ({r['category']})\n")
            f.write(f"Pollutant: {r['dominant_pollutant']}\n")
            f.write(f"Fetched:   {r['fetched_at']}\n")
            f.write("-" * 60 + "\n")

    return len(rows)


# ==============================================================================
# SECTION 12: CITY COMPARISON
# ==============================================================================

def compare_cities(client: AQIClient, cities: List[str]) -> List[AQIReading]:
    """
    Fetch AQI for multiple cities and return the readings sorted from
    cleanest air to worst. Cities that fail to fetch are skipped with a
    warning (rather than crashing the whole comparison).
    """
    readings: List[AQIReading] = []

    for city in cities:
        try:
            reading = client.fetch(city)
            readings.append(reading)
        except AQIToolkitError as e:
            log.warning("Skipping '%s' in comparison: %s", city, e)
            print(f"  (!) Skipped '{city}': {e}")

    # Sort by AQI ascending; unknown/missing AQI values go last.
    readings.sort(key=lambda r: (r.aqi is None, r.aqi if r.aqi is not None else 0))
    return readings


# ==============================================================================
# SECTION 13: WATCHLIST MANAGER
# ==============================================================================

class WatchlistAlert:
    """A simple container describing one triggered alert."""

    def __init__(self, city: str, aqi: int, threshold: int, category: str):
        self.city = city
        self.aqi = aqi
        self.threshold = threshold
        self.category = category

    def __str__(self) -> str:
        return (f"⚠ {self.city}: AQI {self.aqi} ({self.category}) "
                f"exceeds your threshold of {self.threshold}")


def check_watchlist(client: AQIClient, config: ConfigManager) -> List[WatchlistAlert]:
    """Check every city on the watchlist and return a list of alerts for
    any city whose AQI is at or above the configured threshold."""
    alerts: List[WatchlistAlert] = []
    threshold = config.alert_threshold

    for city in config.watchlist:
        try:
            reading = client.fetch(city)
        except AQIToolkitError as e:
            log.warning("Watchlist check failed for '%s': %s", city, e)
            continue

        if reading.aqi is not None and reading.aqi >= threshold:
            alerts.append(WatchlistAlert(reading.city, reading.aqi, threshold, reading.category))

    return alerts


# ==============================================================================
# SECTION 14: CONSOLE FORMATTING HELPERS
# ==============================================================================

def print_header(title: str) -> None:
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_divider() -> None:
    print("-" * 60)


def print_table(headers: List[str], rows: List[List[Any]]) -> None:
    """Print a simple, evenly-spaced ASCII table (no external libraries)."""
    if not rows:
        print("  (no data)")
        return

    # Compute the widest value needed for each column.
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def format_row(cells: List[Any]) -> str:
        return "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print(format_row(headers))
    print(format_row(["-" * w for w in widths]))
    for row in rows:
        print(format_row(row))


def print_reading(reading: AQIReading) -> None:
    print_header(reading.city)
    print(f"  AQI:                {reading.aqi}  ({reading.category})")
    print(f"  Dominant pollutant:  {reading.dominant_pollutant}")
    print(f"  PM2.5:               {reading.pm25}")
    print(f"  PM10:                {reading.pm10}")
    print(f"  Temperature:         {reading.temperature} C")
    print(f"  Humidity:            {reading.humidity} %")
    print(f"  Station time:        {reading.station_time}")
    print_divider()
    print(f"  {reading.health_message}")
    print("=" * 60 + "\n")


# ==============================================================================
# SECTION 15: INTERACTIVE MENU
# ==============================================================================

class InteractiveApp:
    """Runs the numbered, menu-driven version of the toolkit."""

    def __init__(self):
        self.config = ConfigManager()
        self.cache = CacheManager(ttl_minutes=self.config.cache_ttl_minutes)
        self.client = AQIClient(self.config.api_token, cache=self.cache)
        self.db = HistoryDatabase()

    def run(self) -> None:
        print_header(f"{APP_NAME} v{APP_VERSION}")
        print("  Type the number of an option, or 'q' to quit.\n")

        menu = {
            "1": ("Check AQI for a city", self.action_check),
            "2": ("Compare multiple cities", self.action_compare),
            "3": ("View history & stats for a city", self.action_history),
            "4": ("Manage watchlist", self.action_watchlist_menu),
            "5": ("Check watchlist alerts now", self.action_check_alerts),
            "6": ("Export history", self.action_export),
            "7": ("Settings", self.action_settings),
        }

        while True:
            print("Menu:")
            for key, (label, _) in menu.items():
                print(f"  {key}. {label}")
            print("  q. Quit")

            choice = input("\n> ").strip().lower()

            if choice in ("q", "quit", "exit"):
                print("Goodbye!")
                break

            action = menu.get(choice)
            if action is None:
                print("Not a valid option, try again.\n")
                continue

            try:
                action[1]()
            except AQIToolkitError as e:
                print(f"\nError: {e}\n")
            print()

    # ---- menu actions -------------------------------------------------

    def action_check(self) -> None:
        city = input("City name: ").strip() or self.config.default_city
        reading = self.client.fetch(city)
        self.db.add(reading)
        print_reading(reading)

    def action_compare(self) -> None:
        raw = input("City names, separated by commas: ").strip()
        cities = [c.strip() for c in raw.split(",") if c.strip()]
        if not cities:
            print("No cities entered.")
            return

        print("\nFetching...")
        readings = compare_cities(self.client, cities)
        for r in readings:
            self.db.add(r)

        rows = [[r.city, r.aqi, r.category] for r in readings]
        print_table(["City", "AQI", "Category"], rows)

        if readings:
            print(f"\nCleanest air: {readings[0].city} (AQI {readings[0].aqi})")
            print(f"Worst air:    {readings[-1].city} (AQI {readings[-1].aqi})")

    def action_history(self) -> None:
        city = input("City name (leave blank for all cities): ").strip()

        rows = self.db.all_for_city(city) if city else self.db.all()
        if not rows:
            print("No history yet for that search.")
            return

        table_rows = [[r["fetched_at"], r["city"], r["aqi"], r["category"]] for r in rows[:20]]
        print_table(["Fetched At", "City", "AQI", "Category"], table_rows)

        if city:
            stats = compute_city_stats(rows)
            print(f"\nStats for '{city}' ({stats['count']} readings):")
            print(f"  Average AQI: {stats['average']}")
            print(f"  Minimum AQI: {stats['minimum']}")
            print(f"  Maximum AQI: {stats['maximum']}")
            print(f"  Trend:       {stats['trend']}")

    def action_watchlist_menu(self) -> None:
        print(f"Current watchlist: {', '.join(self.config.watchlist) or '(empty)'}")
        sub = input("Type 'add <city>', 'remove <city>', or press Enter to go back: ").strip()

        if not sub:
            return

        parts = sub.split(maxsplit=1)
        if len(parts) != 2:
            print("Please use the format: add <city>  OR  remove <city>")
            return

        command, city = parts[0].lower(), parts[1]

        if command == "add":
            added = self.config.add_to_watchlist(city)
            print(f"Added '{city}' to watchlist." if added else f"'{city}' is already on your watchlist.")
        elif command == "remove":
            removed = self.config.remove_from_watchlist(city)
            print(f"Removed '{city}' from watchlist." if removed else f"'{city}' wasn't on your watchlist.")
        else:
            print("Unknown command.")

    def action_check_alerts(self) -> None:
        if not self.config.watchlist:
            print("Your watchlist is empty. Add cities from the watchlist menu first.")
            return

        print("Checking watchlist...")
        alerts = check_watchlist(self.client, self.config)

        if not alerts:
            print(f"All clear! Nothing on your watchlist is above AQI {self.config.alert_threshold}.")
        else:
            for alert in alerts:
                print(alert)

    def action_export(self) -> None:
        fmt = input("Export format (csv / json / text): ").strip().lower()
        path = input("Output file path: ").strip()

        if not path:
            print("No file path entered.")
            return

        rows = self.db.all(limit=10_000)
        if not rows:
            print("No history to export yet.")
            return

        if fmt == "csv":
            count = export_to_csv(rows, path)
        elif fmt == "json":
            count = export_to_json(rows, path)
        elif fmt == "text":
            count = export_to_text_report(rows, path)
        else:
            print("Unknown format. Choose csv, json, or text.")
            return

        print(f"Exported {count} rows to {path}")

    def action_settings(self) -> None:
        print(f"  API token:         {self.config.api_token}")
        print(f"  Default city:      {self.config.default_city}")
        print(f"  Cache TTL (mins):  {self.config.cache_ttl_minutes}")
        print(f"  Alert threshold:   {self.config.alert_threshold}")

        change = input("\nChange a setting? (token / city / threshold / no): ").strip().lower()

        if change == "token":
            token = input("New API token: ").strip()
            self.config.set("api_token", token)
            print("Token updated.")
        elif change == "city":
            city = input("New default city: ").strip()
            self.config.set("default_city", city)
            print("Default city updated.")
        elif change == "threshold":
            try:
                threshold = int(input("New alert threshold (0-500): ").strip())
                self.config.set("alert_threshold", threshold)
                print("Threshold updated.")
            except ValueError:
                print("Please enter a whole number.")


# ==============================================================================
# SECTION 16: COMMAND-LINE INTERFACE (ARGPARSE)
# ==============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aqi_toolkit.py",
        description=f"{APP_NAME} v{APP_VERSION} - track and compare Air Quality Index data.",
    )
    parser.add_argument("--test", action="store_true", help="Run the built-in test suite and exit.")

    subparsers = parser.add_subparsers(dest="command")

    check_parser = subparsers.add_parser("check", help="Check AQI for one city.")
    check_parser.add_argument("city", help="City name, e.g. london")

    compare_parser = subparsers.add_parser("compare", help="Compare AQI across cities.")
    compare_parser.add_argument("cities", nargs="+", help="Two or more city names")

    history_parser = subparsers.add_parser("history", help="Show search history.")
    history_parser.add_argument("--city", default=None, help="Filter history by city")
    history_parser.add_argument("--limit", type=int, default=20, help="Max rows to show")

    stats_parser = subparsers.add_parser("stats", help="Show statistics for a city's history.")
    stats_parser.add_argument("city", help="City name")

    watch_parser = subparsers.add_parser("watch", help="Manage your watchlist.")
    watch_sub = watch_parser.add_subparsers(dest="watch_action")
    watch_add = watch_sub.add_parser("add", help="Add a city to the watchlist")
    watch_add.add_argument("city")
    watch_remove = watch_sub.add_parser("remove", help="Remove a city from the watchlist")
    watch_remove.add_argument("city")
    watch_sub.add_parser("list", help="List watchlist cities")
    watch_sub.add_parser("check", help="Check all watchlist cities for alerts")

    export_parser = subparsers.add_parser("export", help="Export history to a file.")
    export_parser.add_argument("format", choices=["csv", "json", "text"])
    export_parser.add_argument("path", help="Output file path")

    config_parser = subparsers.add_parser("config", help="View or change settings.")
    config_parser.add_argument("--set-token", default=None)
    config_parser.add_argument("--set-default-city", default=None)
    config_parser.add_argument("--set-threshold", type=int, default=None)
    config_parser.add_argument("--show", action="store_true")

    return parser


def run_cli(args: argparse.Namespace) -> int:
    """Handle a single non-interactive command. Returns an exit code."""
    config = ConfigManager()
    cache = CacheManager(ttl_minutes=config.cache_ttl_minutes)
    client = AQIClient(config.api_token, cache=cache)
    db = HistoryDatabase()

    try:
        if args.command == "check":
            reading = client.fetch(args.city)
            db.add(reading)
            print_reading(reading)

        elif args.command == "compare":
            readings = compare_cities(client, args.cities)
            for r in readings:
                db.add(r)
            rows = [[r.city, r.aqi, r.category] for r in readings]
            print_table(["City", "AQI", "Category"], rows)

        elif args.command == "history":
            rows = db.all_for_city(args.city) if args.city else db.all(limit=args.limit)
            table_rows = [[r["fetched_at"], r["city"], r["aqi"], r["category"]]
                          for r in rows[:args.limit]]
            print_table(["Fetched At", "City", "AQI", "Category"], table_rows)

        elif args.command == "stats":
            rows = db.all_for_city(args.city)
            stats = compute_city_stats(rows)
            print_header(f"Stats for {args.city}")
            for key, value in stats.items():
                print(f"  {key.capitalize()}: {value}")

        elif args.command == "watch":
            if args.watch_action == "add":
                added = config.add_to_watchlist(args.city)
                print("Added." if added else "Already on the watchlist.")
            elif args.watch_action == "remove":
                removed = config.remove_from_watchlist(args.city)
                print("Removed." if removed else "Wasn't on the watchlist.")
            elif args.watch_action == "list":
                print(", ".join(config.watchlist) or "(empty)")
            elif args.watch_action == "check":
                alerts = check_watchlist(client, config)
                if not alerts:
                    print("All clear.")
                for alert in alerts:
                    print(alert)
            else:
                print("Specify: add / remove / list / check")

        elif args.command == "export":
            rows = db.all(limit=10_000)
            exporter = {"csv": export_to_csv, "json": export_to_json, "text": export_to_text_report}
            count = exporter[args.format](rows, args.path)
            print(f"Exported {count} rows to {args.path}")

        elif args.command == "config":
            if args.set_token:
                config.set("api_token", args.set_token)
                print("API token updated.")
            if args.set_default_city:
                config.set("default_city", args.set_default_city)
                print("Default city updated.")
            if args.set_threshold is not None:
                config.set("alert_threshold", args.set_threshold)
                print("Alert threshold updated.")
            if args.show or not any([args.set_token, args.set_default_city, args.set_threshold]):
                print(f"API token:       {config.api_token}")
                print(f"Default city:    {config.default_city}")
                print(f"Watchlist:       {', '.join(config.watchlist) or '(empty)'}")
                print(f"Alert threshold: {config.alert_threshold}")

        else:
            return 1  # unrecognized/no command; caller falls back to interactive mode

    except AQIToolkitError as e:
        print(f"Error: {e}")
        return 1

    return 0


# ==============================================================================
# SECTION 17: BUILT-IN TEST SUITE
# ==============================================================================
# These tests only exercise pure logic (no network calls), so they're safe
# and fast to run any time with: python aqi_toolkit.py --test

class TestAQIClassification(unittest.TestCase):

    def test_good_air(self):
        category, color, _ = classify_aqi(25)
        self.assertEqual(category, "Good")
        self.assertEqual(color, "green")

    def test_moderate_air(self):
        category, _, _ = classify_aqi(75)
        self.assertEqual(category, "Moderate")

    def test_hazardous_air(self):
        category, _, _ = classify_aqi(400)
        self.assertEqual(category, "Hazardous")

    def test_unknown_for_none(self):
        category, _, _ = classify_aqi(None)
        self.assertEqual(category, "Unknown")

    def test_unknown_for_garbage_input(self):
        category, _, _ = classify_aqi("not-a-number")
        self.assertEqual(category, "Unknown")

    def test_boundary_values(self):
        self.assertEqual(classify_aqi(50)[0], "Good")
        self.assertEqual(classify_aqi(51)[0], "Moderate")
        self.assertEqual(classify_aqi(500)[0], "Hazardous")


class TestAQIReading(unittest.TestCase):

    def make_reading(self, aqi=42):
        return AQIReading(
            city="Testville", aqi=aqi, dominant_pollutant="pm25",
            pm25=42, pm10=30, temperature=20, humidity=50,
            station_time="2026-01-01 00:00:00",
        )

    def test_category_property(self):
        reading = self.make_reading(aqi=42)
        self.assertEqual(reading.category, "Good")

    def test_to_dict_includes_category(self):
        reading = self.make_reading(aqi=120)
        d = reading.to_dict()
        self.assertEqual(d["category"], "Unhealthy for Sensitive Groups")

    def test_to_row_length(self):
        reading = self.make_reading()
        self.assertEqual(len(reading.to_row()), len(AQIReading.ROW_HEADERS))


class TestStatistics(unittest.TestCase):

    def test_empty_rows(self):
        stats = compute_city_stats([])
        self.assertEqual(stats["count"], 0)
        self.assertIsNone(stats["average"])

    def test_average_and_bounds(self):
        rows = [{"aqi": 10}, {"aqi": 20}, {"aqi": 30}]
        stats = compute_city_stats(rows)
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["average"], 20)
        self.assertEqual(stats["minimum"], 10)
        self.assertEqual(stats["maximum"], 30)

    def test_trend_worsening(self):
        # newest first, as the real DB returns them
        rows = [{"aqi": 100}, {"aqi": 50}]
        stats = compute_city_stats(rows)
        self.assertEqual(stats["trend"], "worsening")

    def test_trend_improving(self):
        rows = [{"aqi": 20}, {"aqi": 80}]
        stats = compute_city_stats(rows)
        self.assertEqual(stats["trend"], "improving")

    def test_ignores_bad_values(self):
        rows = [{"aqi": "n/a"}, {"aqi": 40}]
        stats = compute_city_stats(rows)
        self.assertEqual(stats["count"], 1)


class TestCompareCities(unittest.TestCase):

    class FakeClient:
        """A stand-in for AQIClient that returns fixed data with no network calls."""

        def __init__(self, fixed_values):
            self.fixed_values = fixed_values

        def fetch(self, city):
            if city not in self.fixed_values:
                raise CityNotFoundError(f"no data for {city}")
            return AQIReading(
                city=city, aqi=self.fixed_values[city], dominant_pollutant="pm25",
                pm25=1, pm10=1, temperature=1, humidity=1, station_time="now",
            )

    def test_sorts_ascending_by_aqi(self):
        client = self.FakeClient({"clean": 10, "dirty": 300, "mid": 100})
        result = compare_cities(client, ["dirty", "clean", "mid"])
        self.assertEqual([r.city for r in result], ["clean", "mid", "dirty"])

    def test_skips_unfetchable_cities(self):
        client = self.FakeClient({"clean": 10})
        result = compare_cities(client, ["clean", "ghosttown"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].city, "clean")


def run_tests() -> int:
    """Run the built-in test suite and return a process exit code."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for test_class in (TestAQIClassification, TestAQIReading, TestStatistics, TestCompareCities):
        suite.addTests(loader.loadTestsFromTestCase(test_class))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


# ==============================================================================
# SECTION 18: ENTRY POINT
# ==============================================================================

def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.test:
        return run_tests()

    if args.command:
        return run_cli(args)

    # No subcommand given -> fall back to the friendly interactive menu.
    InteractiveApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
