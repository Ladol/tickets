import os
import random
import time
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

LISBON_TZ = ZoneInfo("Europe/Lisbon")

API_BASE = "https://api-gateway.cp.pt/cp/services"
OIDC_BASE = "https://login.cp.pt/realms/cpclients/protocol/openid-connect"
OIDC_CLIENT_ID = "websitecp"
OIDC_REDIRECT_URI = "https://cp.pt/en/login-check"

# (connect, read) timeouts. Reads are generous because CP's gateway slows to a crawl at the 24h unlock mark.
API_TIMEOUT = (10, 30)

# CP holds a created sale (and its seat) for 15 minutes.
SEAT_LOCK_DURATION = timedelta(minutes=15)
# Stop retrying this long before the lock is due to expire (also absorbs the latency of the create-sale call).
SEAT_LOCK_MARGIN = timedelta(seconds=60)

# statusCode values in the train-seats map (1 = taken, 3 = blocked, e.g. wheelchair spaces)
SEAT_FREE = 0
SEAT_CURRENT = 2
# placeType values of non-seat places: 0 = aisle / empty space, 4 and 5 = the two halves of a table
TABLE_PLACE_TYPES = {4, 5}

# CP error codes (the "error" field of an error response) that mean the train has no free seats.
# CP answers these with HTTP 500, so without this they would look like a transient server error.
CP_NO_SEATS_ERRORS = {"WS:RES:116"}  # "Não há lugares disponíveis para a totalidade do pedido"

# Seat preference engine: preferred seat numbers (best first) within preferred carriages
PREFERRED_SEATS = [114, 118, 117, 113, 14, 27, 23, 28, 24]
TIER_1_SEATS = [114, 118, 117, 113]
PREFERRED_CARRIAGES = set(range(21, 26))  # Carriages 21, 22, 23, 24, 25


class CPError(Exception):
    """A failed CP request, classified so callers can decide whether retrying makes sense."""

    def __init__(self, message: str, status: int = None, transient: bool = True, ambiguous: bool = False, cp_code: str = None):
        super().__init__(message)
        self.status = status
        self.cp_code = cp_code  # e.g. "WS:RES:116"
        # transient: worth retrying (timeouts, 5xx, rate limits, expired token)
        self.transient = transient
        # ambiguous: CP may have processed the request even though we never saw a success response
        self.ambiguous = ambiguous


class NoSeatAvailable(Exception):
    """CP refused to create a sale (train full, or reservations not open)."""


class PurchaseUncertain(Exception):
    """Confirmation may or may not have gone through; never retry blindly after this."""


class BookingAborted(Exception):
    """The user aborted the booking from Discord."""


class _SeatLockExpired(Exception):
    """Checkout kept failing until the 15-minute seat lock ran out."""


def _now() -> datetime:
    return datetime.now(LISBON_TZ)


def _backoff(attempt: int, cap: float = 15.0) -> float:
    """Exponential backoff with jitter: ~2s, 3s, 5s, 8s, 13s, then `cap`."""
    return min(cap, 2.0 * 1.6 ** (attempt - 1)) + random.uniform(0, 1)


# --- SEAT MAP HELPERS ---

def parse_carriages(seat_map: dict) -> list[dict]:
    """
    Normalizes CP's train-seats response into [{"number", "rows": [[place, ...], ...]}], where place is
    {"seat", "status", "table", "window", "type"} ("seat" is None for aisles, gaps and tables).

    CP's rows run along the car (e.g. 5 rows: window, aisle-side, the aisle itself, aisle-side, window),
    so the outermost rows that contain seats are the window seats.
    """
    carriages = []
    for carriage in seat_map.get("carriages", []):
        try:
            number = int(carriage.get("number", 0))
        except (ValueError, TypeError):
            number = 0
        raw_rows = [row.get("places", []) for row in carriage.get("rows", [])]
        seat_rows = [i for i, places in enumerate(raw_rows) if any(p.get("seatNumber") is not None for p in places)]
        window_rows = {seat_rows[0], seat_rows[-1]} if seat_rows else set()
        rows = [
            [
                {
                    "seat": place.get("seatNumber"),
                    "status": place.get("statusCode"),
                    "table": bool(place.get("withTable")),
                    "window": i in window_rows,
                    "type": place.get("placeType"),
                }
                for place in places
            ]
            for i, places in enumerate(raw_rows)
        ]
        carriages.append({"number": number, "rows": rows})
    return carriages


def seat_details(carriages: list[dict]) -> dict:
    """Maps (carriage, seat) to its place dict, for describing seats (window / table)."""
    return {
        (carriage["number"], place["seat"]): place
        for carriage in carriages for row in carriage["rows"] for place in row
        if place["seat"] is not None
    }


def iter_seats(carriages: list[dict]):
    """Yields (carriage_number, seat_number, status) for every real seat."""
    for carriage in carriages:
        for row in carriage["rows"]:
            for place in row:
                if place["seat"] is not None:
                    yield carriage["number"], place["seat"], place["status"]


def find_current_seat(carriages: list[dict]) -> dict | None:
    for c_num, s_num, status in iter_seats(carriages):
        if status == SEAT_CURRENT:
            return {"carriage": c_num, "seat": s_num}
    return None


def free_seats(carriages: list[dict], carriage_number: int = None) -> list[dict]:
    return [
        {"carriage": c_num, "seat": s_num}
        for c_num, s_num, status in iter_seats(carriages)
        if status == SEAT_FREE and (carriage_number is None or c_num == carriage_number)
    ]


def _error_details(response: requests.Response) -> tuple[str | None, str]:
    """
    Extracts (CP error code, readable text) from an error response. CP's errors look like
    {"status": 500, "error": "WS:RES:116", "description": "Atenção\\nNão há lugares ...", "message": "..."}.
    """
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        text = data.get("description") or data.get("message") or ""
        text = " ".join(text.replace("Atenção", "").split())
        return str(data["error"]), f"{text} [{data['error']}]"[:300]
    return None, " ".join(response.text.split())[:300]


def _seat_score(seat: dict) -> int:
    if seat["seat"] not in PREFERRED_SEATS or seat["carriage"] not in PREFERRED_CARRIAGES:
        return -9999
    score = seat["carriage"] * 100
    if seat["seat"] in TIER_1_SEATS:
        score += 150
    score -= PREFERRED_SEATS.index(seat["seat"])
    return score


class CPClient:
    """Client for Comboios de Portugal (CP) APIs."""

    def __init__(
        self,
        email: str = None,
        password: str = None,
        mobile: str = None,
        name: str = None,
        passenger_id: str = None,
        green_pass_number: str = None,
    ):
        self.email = email or os.getenv("CP_EMAIL")
        self.password = password or os.getenv("CP_PASSWORD")
        self.mobile = mobile or os.getenv("CP_MOBILE")
        self.name = name or os.getenv("CP_NAME")
        self.passenger_id = passenger_id or os.getenv("CP_PASSENGER_ID")
        self.green_pass_number = green_pass_number or os.getenv("CP_GREEN_PASS")

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.refresh_token = None
        self.token_expires_at = 0.0  # time.monotonic() deadline of the current access token
        self.stations_cache = []

    # --- HTTP ---

    def _request(self, method: str, url: str, *, auth: bool = True, timeout=API_TIMEOUT, **kwargs) -> requests.Response:
        """Sends a request, refreshing the access token first if needed, and raises CPError on failure."""
        if auth:
            self.ensure_fresh_token()

        endpoint = urlparse(url).path.rsplit("/services/", 1)[-1]
        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.ConnectTimeout as e:
            raise CPError(f"{method} {endpoint}: connection timed out") from e
        except requests.ReadTimeout as e:
            raise CPError(f"{method} {endpoint}: CP did not answer in time", ambiguous=True) from e
        except requests.ConnectionError as e:
            raise CPError(f"{method} {endpoint}: connection error ({str(e)[:150]})", ambiguous=True) from e

        if response.status_code >= 400:
            status = response.status_code
            if status in (401, 403):
                self.token_expires_at = 0.0  # force a token refresh before the next attempt
            cp_code, detail = _error_details(response)
            if cp_code in CP_NO_SEATS_ERRORS:
                raise CPError(f"No seats available: {detail}", status=status, transient=False, cp_code=cp_code)
            raise CPError(
                f"{method} {endpoint}: HTTP {status} {detail}",
                status=status,
                transient=status >= 500 or status in (401, 403, 408, 429),
                ambiguous=status in (502, 504),  # the gateway gave up, the backend may still have done it
                cp_code=cp_code,
            )
        return response

    # --- CONFIGURATION & STATION DATA ---

    def fetch_fe_config(self) -> dict:
        """Fetches frontend session keys and updates headers."""
        data = self._request("GET", "https://cp.pt/fe-config.json", auth=False).json()

        self.session.headers.update({
            "x-cp-connect-id": data.get("xcck"),
            "x-cp-connect-secret": data.get("xccs"),
            "X-Api-Key": data.get("ticketingApiKey"),
            "Origin": "https://cp.pt",
            "Referer": "https://cp.pt/",
        })
        return data

    def get_stations(self, force_refresh: bool = False) -> list[dict]:
        """Fetches and caches the list of CP stations."""
        if self.stations_cache and not force_refresh:
            return self.stations_cache

        if "X-Api-Key" not in self.session.headers:
            self.fetch_fe_config()

        self.stations_cache = self._request("GET", f"{API_BASE}/travel-api/stations", auth=False).json()
        return self.stations_cache

    def get_station_code(self, station_name: str) -> str | None:
        """Resolves a human-readable station designation to its CP station code."""
        stations = self.get_stations()
        for station in stations:
            if station.get("designation") == station_name:
                return station.get("code")
        return None

    # --- JOURNEY SEARCH & ORIGIN TIMES ---

    def search_journeys(self, dep_code: str, arr_code: str, date_str: str) -> list[dict]:
        """Searches available Intercidades (IC) train journeys for a route and date."""
        if "X-Api-Key" not in self.session.headers:
            self.fetch_fe_config()

        payload = {
            "departureStationCode": dep_code,
            "arrivalStationCode": arr_code,
            "travelDate": date_str,
            "returnDate": None,
            "classes": [2],
            "configID": 200,
            "lang": "PT",
            "quantities": [{"quantity": 1, "type": 1}],
            "searchType": 3,
            "services": ["IC"],
            "timeLimit": {"startTime": "00:00", "endTime": "23:59", "limitType": 0},
            "returnTimeLimit": {"startTime": "00:00", "endTime": "23:59", "limitType": 0},
            "saleableOnly": False,
            "username": "sivNetticket",
        }
        res = self._request("POST", f"{API_BASE}/travel-api/journeys", auth=False, json=payload)
        return res.json().get("outwardTrip", [])

    @staticmethod
    def get_origin_time(train_num: int | str, date_str: str) -> str | None:
        """Queries Infraestruturas de Portugal API for exact origin departure time."""
        url = f"https://www.infraestruturasdeportugal.pt/negocios-e-servicos/horarios-ncombio/{train_num}/{date_str}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.infraestruturasdeportugal.pt/negocios-e-servicos/horarios",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            return res.json().get("response", {}).get("DataHoraOrigem")
        except Exception as e:
            print(f"[!] Error fetching IP API for Train {train_num}: {e}")
            return None

    # --- AUTHENTICATION & SESSIONS ---

    def login(self, email: str = None, password: str = None):
        """Performs automated Keycloak OIDC login and retrieves access/refresh tokens."""
        email = email or self.email
        password = password or self.password

        if not email or not password:
            raise ValueError("CP login credentials (email/password) are missing.")

        # A leftover Keycloak SSO cookie would skip the login form, so always start from a clean jar.
        self.session.cookies.clear()

        auth_params = {
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": OIDC_REDIRECT_URI,
            "response_type": "code",
            "response_mode": "fragment",
            "scope": "openid",
        }
        response = self._request("GET", f"{OIDC_BASE}/auth", auth=False, params=auth_params)

        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", id="kc-form-login")
        if not form:
            raise CPError("Could not find Keycloak login form. CP login may be overloaded or its interface changed.")

        action_url = form.get("action")
        login_payload = {"username": email, "password": password, "credentialId": ""}
        post_response = self._request("POST", action_url, auth=False, data=login_payload, allow_redirects=False)

        if post_response.status_code not in [302, 303]:
            # Keycloak re-renders the form (HTTP 200) on bad credentials. Never retry those: it can lock the account.
            raise CPError(
                f"Login rejected (HTTP {post_response.status_code}). Check CP_EMAIL / CP_PASSWORD.",
                status=post_response.status_code,
                transient=False,
            )

        redirect_location = post_response.headers.get("Location")
        if not redirect_location:
            raise CPError("No redirect location received after login.")

        parsed_url = urlparse(redirect_location)
        fragment_data = parse_qs(parsed_url.fragment)
        auth_code = fragment_data.get("code")

        if not auth_code:
            raise CPError("Failed to extract authorization code from redirect fragment.")

        token_payload = {
            "grant_type": "authorization_code",
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": OIDC_REDIRECT_URI,
            "code": auth_code[0],
        }
        token_headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        token_response = self._request("POST", f"{OIDC_BASE}/token", auth=False, data=token_payload, headers=token_headers)

        self._store_tokens(token_response.json())
        self.session.headers.update({"x-cp-client-id": email})

    def refresh_auth_token(self):
        """Exchanges the refresh token for a new access token."""
        if not self.refresh_token:
            raise CPError("Cannot refresh token: No refresh token is currently saved.", transient=False)

        token_payload = {
            "grant_type": "refresh_token",
            "client_id": OIDC_CLIENT_ID,
            "refresh_token": self.refresh_token,
        }
        token_headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        response = self._request("POST", f"{OIDC_BASE}/token", auth=False, data=token_payload, headers=token_headers)
        self._store_tokens(response.json())

    def _store_tokens(self, token_data: dict):
        self.session.headers.update({"x-access-token": token_data.get("access_token")})
        self.refresh_token = token_data.get("refresh_token")
        self.token_expires_at = time.monotonic() + int(token_data.get("expires_in", 300))

    def ensure_fresh_token(self):
        """Refreshes the access token (5 min lifetime) when it is close to expiring; logs in again if refresh is refused."""
        if not self.refresh_token or time.monotonic() < self.token_expires_at - 45:
            return
        try:
            self.refresh_auth_token()
        except CPError as e:
            if e.transient:
                raise
            print(f"[CP] Token refresh refused ({e}); logging in again...")
            self.login()

    # --- BOOKING & SEAT SELECTION ---

    def create_sale(self, train_num: int, train_date: str, dep_code: str, arr_code: str) -> str:
        """Initializes a sale reservation, holding a seat for 15 minutes."""
        sale_payload = {
            "quantity": 1,
            "travelClass": {"code": "2"},
            "travelDate": train_date,
            "outwardTrip": [{
                "trainNumber": int(train_num),
                "departureStation": {"code": dep_code},
                "arrivalStation": {"code": arr_code},
                "serviceCode": {"code": "IC", "designation": "Intercidades"},
            }],
            "lang": "pt",
        }
        response = self._request("POST", f"{API_BASE}/ticketing-api/sale", json=sale_payload)
        sale_id = response.json().get("saleID")
        if not sale_id:
            raise CPError("CP did not return a saleID for this reservation.", transient=False)
        return str(sale_id)

    def get_seat_map(self, sale_id: str, train_num: int) -> dict:
        """Fetches the raw seat map of the train for this sale (see parse_carriages)."""
        return self._request("GET", f"{API_BASE}/ticketing-api/train-seats/{sale_id}/trains/{train_num}").json()

    def move_seat(self, sale_id: str, train_num: int, current: dict, target: dict):
        """Moves the sale's seat from `current` to `target` ({"carriage", "seat"} dicts)."""
        seat_payload = {
            "originalSeats": [{
                "carriageNumber": current["carriage"],
                "seatNumber": current["seat"],
                "trainNumber": int(train_num),
            }],
            "requestedSeats": [{
                "carriageNumber": target["carriage"],
                "seatNumber": target["seat"],
                "trainNumber": int(train_num),
            }],
        }
        self._request("PUT", f"{API_BASE}/ticketing-api/train-seats/{sale_id}", json=seat_payload)

    def change_seat(self, sale_id: str, train_num: int) -> dict:
        """Inspects the seat map and switches to a preferred seat if available."""
        try:
            carriages = parse_carriages(self.get_seat_map(sale_id, train_num))
        except Exception as e:
            return {"status": "unchanged", "message": f"Could not fetch seat map: {e}"}

        current_seat = find_current_seat(carriages)
        if not current_seat:
            return {"status": "unchanged", "message": "Could not identify current seat in map."}

        candidates = [s for s in free_seats(carriages) if _seat_score(s) > -9999]
        if not candidates:
            return {
                "status": "kept",
                "seat": current_seat,
                "message": f"No preferred free seats in carriages 21-25. Keeping initial Car {current_seat['carriage']}, Seat {current_seat['seat']}.",
            }

        best_seat = max(candidates, key=_seat_score)
        if _seat_score(best_seat) <= _seat_score(current_seat):
            return {
                "status": "optimal",
                "seat": current_seat,
                "message": f"Current seat Car {current_seat['carriage']}, Seat {current_seat['seat']} is already optimal.",
            }

        for attempt in range(2):
            try:
                self.move_seat(sale_id, train_num, current_seat, best_seat)
                return {
                    "status": "changed",
                    "seat": best_seat,
                    "message": f"Upgraded seat to Car {best_seat['carriage']}, Seat {best_seat['seat']}!",
                }
            except Exception:
                time.sleep(1)

        return {
            "status": "failed_change",
            "seat": current_seat,
            "message": f"Seat change attempts failed. Retained Car {current_seat['carriage']}, Seat {current_seat['seat']}.",
        }

    def set_passenger_info(self, sale_id: str):
        """Attaches passenger Citizen Card (CC) details."""
        payload = {
            "salePassengers": [{
                "idtype": {"code": "CC", "designation": "Cartão de Cidadão"},
                "passengerID": self.passenger_id,
                "passengerName": self.name,
            }]
        }
        self._request("PUT", f"{API_BASE}/ticketing-api/sale/{sale_id}/passengers", json=payload)

    def apply_discount(self, sale_id: str):
        """Applies Passe Ferroviário Verde (Green Rail Pass, code 302)."""
        payload = {
            "requestedItems": [{
                "itemCode": "302",  # Passe Ferroviário Verde
                "relatedTrain": None,
                "ticketIndex": 0,
                "type": "DISCOUNT",
                "inputData": self.green_pass_number,
            }]
        }
        self._request("PUT", f"{API_BASE}/ticketing-api/sale/{sale_id}/items", json=payload)

    def attach_client_info(self, sale_id: str):
        """Attaches client contact info."""
        payload = {
            "clientEmail": self.email,
            "clientID": self.email,
            "clientMobile": self.mobile,
            "clientName": self.name,
        }
        self._request("PUT", f"{API_BASE}/ticketing-api/sale/{sale_id}/client", json=payload)

    def confirm_purchase(self, sale_id: str) -> str:
        """Confirms the purchase and extracts the ticket PDF URL."""
        response = self._request("PUT", f"{API_BASE}/ticketing-api/sale/{sale_id}/confirm", headers={"Content-Length": "0"})

        data = response.json()
        status_code = data.get("status", {}).get("code")
        if status_code != "CONFIRMED":
            raise CPError(f"Purchase not confirmed by CP. Status code: {status_code}", status=response.status_code, transient=False)

        ticket_msg = "Check your email or CP App for your ticket."
        # ticketData is nested in travelData in confirm responses
        ticket_data = data.get("ticketData") or data.get("travelData", {}).get("ticketData")
        if ticket_data and isinstance(ticket_data, list) and len(ticket_data) > 0:
            extracted_url = ticket_data[0].get("ticketURL")
            if extracted_url:
                ticket_msg = f"https://cp.pt{extracted_url}"
        return ticket_msg


# --- ASYNC HIGH-LEVEL BOOKING PIPELINE ---

class Booking:
    """
    One purchase attempt: lock a seat, wait for the 24h unlock mark, then check out.

    Every network step is retried while it can still help. Checkout is retried until the 15-minute
    seat lock is about to run out; then a new seat is locked and checkout starts over (up to
    MAX_RELOCKS times), unless CP may already have confirmed the sale.

    The Discord seat picker reads sale_id / current_seat / can_change_seat and goes through
    fetch_seat_map / move_to, which share io_lock with the pipeline so requests never interleave.

    Modes:
      "scheduled": triggered ~5 min before unlock; retries the seat lock until a few minutes past unlock.
      "watch":     periodic probe of a (possibly full) train; gives up quickly if CP refuses the sale.
    """

    MAX_RELOCKS = 2

    def __init__(self, payload: dict, notify=None, mode: str = "scheduled", client: CPClient = None):
        self.payload = payload
        self.mode = mode
        self.client = client or CPClient()
        self._notify_cb = notify  # async callback(text, key); lines sharing a key replace each other

        self.train_num = int(payload["train_number"])
        self.train_date = payload["train_date"]
        origin_departure = datetime.strptime(
            f"{self.train_date} {payload['unlock_time']}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=LISBON_TZ)
        self.unlock_at = origin_departure - timedelta(hours=24)

        self.state = "starting"  # starting -> locking -> holding -> finalizing -> done
        self.sale_id = None
        self.current_seat = None
        self.lock_expires_at = None
        self.confirm_attempted = False
        self.io_lock = asyncio.Lock()
        self._abort = asyncio.Event()

    @property
    def can_change_seat(self) -> bool:
        return (
            self.sale_id is not None
            and self.state in ("holding", "finalizing")
            and not self.confirm_attempted
            and _now() < self.lock_expires_at
        )

    def abort(self):
        self._abort.set()

    async def notify(self, msg: str, key: str = None):
        print(f"[CP] {msg}")
        if self._notify_cb:
            try:
                await self._notify_cb(msg, key)
            except Exception as e:
                print(f"[!] Notification callback error: {e}")

    async def _sleep(self, seconds: float):
        """Sleeps, waking early (and raising) if the booking is aborted."""
        try:
            await asyncio.wait_for(self._abort.wait(), timeout=max(seconds, 0))
        except asyncio.TimeoutError:
            return
        raise BookingAborted("Booking aborted from Discord.")

    async def _call(self, func, *args):
        if self._abort.is_set():
            raise BookingAborted("Booking aborted from Discord.")
        async with self.io_lock:
            return await asyncio.to_thread(func, *args)

    async def _retry(self, label: str, func, *args, until: datetime, retry_rejections: bool):
        """Calls func until it succeeds or the next attempt would start after `until`."""
        attempt = 0
        while True:
            try:
                return await self._call(func, *args)
            except CPError as e:
                attempt += 1
                if not (e.transient or retry_rejections):
                    raise
                delay = _backoff(attempt, cap=15 if e.transient else 30)
                if _now() + timedelta(seconds=delay) > until:
                    raise
                await self.notify(f"⚠️ {label} failed (attempt {attempt}): {e} — retrying in {delay:.0f}s...", key=label)
                await self._sleep(delay)

    # --- Pipeline ---

    async def run(self) -> str:
        """Runs the whole booking and returns the ticket link (or a pointer to the CP app)."""
        await self.notify(f"🚂 Starting automated booking for Train **{self.train_num}** on **{self.train_date}**...")

        if self.mode == "scheduled" and self.unlock_at - _now() > timedelta(minutes=7):
            # Locking now would let the 15-minute hold expire before checkout is possible.
            lock_at = self.unlock_at - timedelta(minutes=5)
            await self.notify(f"⏳ Trigger fired early; waiting until {lock_at:%H:%M:%S} to lock the seat so the hold lasts past the unlock mark...")
            await self._sleep((lock_at - _now()).total_seconds())

        # Before unlock, keep retrying until a few minutes past it; for watch probes, give up fast.
        setup_until = max(_now(), self.unlock_at) + timedelta(minutes=5 if self.mode == "scheduled" else 1)

        await self.notify("🔑 1/7 Fetching CP session configuration...")
        await self._retry("Fetching CP configuration", self.client.fetch_fe_config, until=setup_until, retry_rejections=False)

        dep_code = self.payload.get("dep_code") or await self._retry(
            "Station lookup", self.client.get_station_code, self.payload["departure_station"], until=setup_until, retry_rejections=False
        )
        arr_code = self.payload.get("arr_code") or await self._retry(
            "Station lookup", self.client.get_station_code, self.payload["arrival_station"], until=setup_until, retry_rejections=False
        )
        if not dep_code or not arr_code:
            raise ValueError(
                f"Could not resolve station codes for '{self.payload['departure_station']}' or '{self.payload['arrival_station']}'."
            )

        await self.notify("🔐 2/7 Logging into CP account...")
        await self._retry("Login", self.client.login, until=setup_until, retry_rejections=False)

        for lock_round in range(self.MAX_RELOCKS + 1):
            await self._lock_seat(dep_code, arr_code, relock=lock_round > 0)
            await self._wait_for_unlock(announce_passed=lock_round == 0)
            try:
                ticket = await self._finalize()
                self.state = "done"
                return ticket
            except _SeatLockExpired as e:
                if lock_round == self.MAX_RELOCKS:
                    raise CPError(f"Checkout kept failing through {lock_round + 1} seat locks. Last error: {e}", transient=False) from e
                await self.notify(
                    f"🔁 Seat lock ran out before checkout went through — locking a new seat "
                    f"(retry {lock_round + 1}/{self.MAX_RELOCKS})..."
                )

    async def _lock_seat(self, dep_code: str, arr_code: str, relock: bool):
        self.state = "locking"
        self.sale_id = None
        self.current_seat = None

        if self.mode == "watch":
            until, retry_rejections = _now() + timedelta(minutes=1), False
        elif relock:
            until, retry_rejections = _now() + timedelta(minutes=3), True
        else:
            # A full train can free up right around the unlock mark, so keep trying for a while past it.
            until, retry_rejections = max(_now(), self.unlock_at) + timedelta(minutes=5), True

        await self.notify(f"💺 3/7 Reserving seat on Train {self.train_num} (locking for 15 minutes)...")
        try:
            sale_id = await self._retry(
                "Seat reservation", self.client.create_sale, self.train_num, self.train_date, dep_code, arr_code,
                until=until, retry_rejections=retry_rejections,
            )
        except CPError as e:
            if not e.transient:
                raise NoSeatAvailable(f"CP refused the reservation: {e}") from e
            raise

        self.sale_id = sale_id
        self.lock_expires_at = _now() + SEAT_LOCK_DURATION

        seat_result = await self._call(self.client.change_seat, self.sale_id, self.train_num)
        self.current_seat = seat_result.get("seat")
        self.state = "holding"
        await self.notify(f"🎯 4/7 Seat assignment: {seat_result.get('message', 'Checked')}")

    async def _wait_for_unlock(self, announce_passed: bool):
        now = _now()
        if now < self.unlock_at:
            wait_seconds = (self.unlock_at - now).total_seconds() + 2
            await self.notify(
                f"⏳ 5/7 Seat is safely held! Pausing for **{int(wait_seconds)}s** until 24h mark "
                f"({self.unlock_at.strftime('%H:%M:%S')} Lisbon time)... Use 🪑 to pick another seat meanwhile."
            )
            await self._sleep(wait_seconds)
            await self.notify("⏰ Unlock mark reached! Waking up to finalize purchase...")
        elif announce_passed:
            await self.notify("⚡ 24h unlock mark has already passed! Proceeding immediately...")

    async def _finalize(self) -> str:
        self.state = "finalizing"
        retry_until = self.lock_expires_at - SEAT_LOCK_MARGIN
        steps = [
            ("Passenger ID", self.client.set_passenger_info),
            ("Green Pass discount", self.client.apply_discount),
            ("Client info", self.client.attach_client_info),
            ("Purchase confirmation", self.client.confirm_purchase),
        ]
        done = set()
        maybe_applied = set()  # steps whose request timed out, so CP may have processed it anyway

        await self.notify("🔄 6/7 Refreshing session authorization token...")
        announced = False
        attempt = 0
        while True:
            label = "Token refresh"
            try:
                await self._call(self.client.ensure_fresh_token)
                if not announced:
                    await self.notify("📝 7/7 Attaching passenger ID, Green Pass discount, and client info...")
                    announced = True

                result = None
                for label, func in steps:
                    if label in done:
                        continue
                    if func == self.client.confirm_purchase:
                        self.confirm_attempted = True
                    try:
                        result = await self._call(func, self.sale_id)
                    except CPError as e:
                        if label in maybe_applied and e.status is not None and not e.transient:
                            if func == self.client.confirm_purchase:
                                raise PurchaseUncertain(
                                    f"A confirmation request timed out and CP then rejected the retry ({e}). "
                                    "The ticket may already be bought: check the CP app / email before booking again."
                                ) from e
                            # The timed-out attempt most likely went through and CP rejects the repeat.
                            await self.notify(f"ℹ️ {label}: CP rejected a repeat of a timed-out request ({e}); assuming the first one went through.")
                            done.add(label)
                            continue
                        if e.ambiguous:
                            maybe_applied.add(label)
                        raise
                    done.add(label)
                return result

            except CPError as e:
                attempt += 1
                delay = _backoff(attempt)
                if _now() + timedelta(seconds=delay) >= retry_until:
                    if "Purchase confirmation" in maybe_applied:
                        raise PurchaseUncertain(
                            f"Confirmation timed out and the seat lock ran out before CP gave a definite answer ({e}). "
                            "Check the CP app / email before booking again."
                        ) from e
                    raise _SeatLockExpired(str(e)) from e
                await self.notify(
                    f"⚠️ {label} failed (attempt {attempt}): {e} — retrying in {delay:.0f}s "
                    f"(seat held until {self.lock_expires_at:%H:%M:%S})...",
                    key="checkout-retry",
                )
                await self._sleep(delay)

    # --- Manual seat selection (Discord seat picker) ---

    async def fetch_seat_map(self) -> list[dict]:
        if self.sale_id is None:
            raise RuntimeError("No seat is locked yet.")
        return parse_carriages(await self._call(self.client.get_seat_map, self.sale_id, self.train_num))

    async def move_to(self, carriage: int, seat: int) -> dict:
        """Moves the locked seat to (carriage, seat). Returns the new seat."""
        if not self.can_change_seat:
            raise RuntimeError("The seat can't be changed right now (not locked yet, lock expired, or already confirming).")

        target = {"carriage": int(carriage), "seat": int(seat)}
        async with self.io_lock:
            carriages = parse_carriages(await asyncio.to_thread(self.client.get_seat_map, self.sale_id, self.train_num))
            current = find_current_seat(carriages)
            if not current:
                raise RuntimeError("Could not identify your current seat in the seat map.")
            if current == target:
                return current
            if target not in free_seats(carriages):
                raise RuntimeError(f"Car {target['carriage']}, Seat {target['seat']} is not free.")
            await asyncio.to_thread(self.client.move_seat, self.sale_id, self.train_num, current, target)
            self.current_seat = target

        await self.notify(f"🪑 Seat changed manually to Car {target['carriage']}, Seat {target['seat']}.")
        return target
