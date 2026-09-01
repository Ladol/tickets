import os
import time
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load local environment variables if available
load_dotenv()

LISBON_TZ = ZoneInfo("Europe/Lisbon")


class CPClient:
    """Client for Comboios de Portugal (CP) APIs and automated booking workflow."""

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
        self.stations_cache = []

    # --- CONFIGURATION & STATION DATA ---

    def fetch_fe_config(self) -> dict:
        """Fetches frontend session keys and updates headers."""
        response = self.session.get("https://cp.pt/fe-config.json", timeout=15)
        response.raise_for_status()
        data = response.json()

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

        url = "https://api-gateway.cp.pt/cp/services/travel-api/stations"
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        self.stations_cache = response.json()
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

        url = "https://api-gateway.cp.pt/cp/services/travel-api/journeys"
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
        res = self.session.post(url, json=payload, timeout=20)
        res.raise_for_status()
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

        auth_url = "https://login.cp.pt/realms/cpclients/protocol/openid-connect/auth"
        auth_params = {
            "client_id": "websitecp",
            "redirect_uri": "https://cp.pt/en/login-check",
            "response_type": "code",
            "response_mode": "fragment",
            "scope": "openid",
        }
        response = self.session.get(auth_url, params=auth_params, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        form = soup.find("form", id="kc-form-login")
        if not form:
            raise Exception("Could not find Keycloak login form. CP login interface may have changed.")

        action_url = form.get("action")
        login_payload = {"username": email, "password": password, "credentialId": ""}
        post_response = self.session.post(action_url, data=login_payload, allow_redirects=False, timeout=15)

        if post_response.status_code not in [302, 303]:
            raise Exception(f"Login failed. Status code: {post_response.status_code}")

        redirect_location = post_response.headers.get("Location")
        if not redirect_location:
            raise Exception("No redirect location received after login.")

        parsed_url = urlparse(redirect_location)
        fragment_data = parse_qs(parsed_url.fragment)
        auth_code = fragment_data.get("code")

        if not auth_code:
            raise Exception("Failed to extract authorization code from redirect fragment.")

        token_url = "https://login.cp.pt/realms/cpclients/protocol/openid-connect/token"
        token_payload = {
            "grant_type": "authorization_code",
            "client_id": "websitecp",
            "redirect_uri": "https://cp.pt/en/login-check",
            "code": auth_code[0],
        }
        token_headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        token_response = self.session.post(token_url, data=token_payload, headers=token_headers, timeout=15)
        token_response.raise_for_status()

        token_data = token_response.json()
        access_token = token_data.get("access_token")
        self.refresh_token = token_data.get("refresh_token")

        self.session.headers.update({
            "x-access-token": access_token,
            "x-cp-client-id": email,
        })

    def refresh_auth_token(self):
        """Refreshes the OAuth access token before final checkout."""
        if not self.refresh_token:
            raise Exception("Cannot refresh token: No refresh token is currently saved.")

        token_url = "https://login.cp.pt/realms/cpclients/protocol/openid-connect/token"
        token_payload = {
            "grant_type": "refresh_token",
            "client_id": "websitecp",
            "refresh_token": self.refresh_token,
        }
        token_headers = {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}
        response = self.session.post(token_url, data=token_payload, headers=token_headers, timeout=15)
        response.raise_for_status()

        token_data = response.json()
        self.session.headers.update({"x-access-token": token_data.get("access_token")})
        self.refresh_token = token_data.get("refresh_token")

    # --- BOOKING & SEAT SELECTION ---

    def create_sale(self, train_num: int, train_date: str, dep_code: str, arr_code: str) -> str:
        """Initializes a sale reservation, holding a seat for 15 minutes."""
        url = "https://api-gateway.cp.pt/cp/services/ticketing-api/sale"
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
        response = self.session.post(url, json=sale_payload, timeout=20)
        response.raise_for_status()
        sale_id = response.json().get("saleID")
        if not sale_id:
            raise Exception("CP did not return a saleID for this reservation.")
        return str(sale_id)

    def change_seat(self, sale_id: str, train_num: int) -> dict:
        """Inspects the seat map and switches to a preferred seat if available."""
        url_get = f"https://api-gateway.cp.pt/cp/services/ticketing-api/train-seats/{sale_id}/trains/{train_num}"

        try:
            resp = self.session.get(url_get, timeout=15)
            resp.raise_for_status()
            seat_map = resp.json()
        except Exception as e:
            return {"status": "unchanged", "message": f"Could not fetch seat map: {e}"}

        pref_list = [114, 118, 117, 113, 14, 27, 23, 28, 24]
        tier_1 = [114, 118, 117, 113]
        allowed_carriages = set(range(21, 26))  # Carriages 21, 22, 23, 24, 25

        current_seat = None
        free_seats = []

        for carriage in seat_map.get("carriages", []):
            try:
                c_num = int(carriage.get("number", 0))
            except (ValueError, TypeError):
                c_num = 0

            for row in carriage.get("rows", []):
                for place in row.get("places", []):
                    s_code = place.get("statusCode")
                    s_num = place.get("seatNumber")

                    if s_num is None:
                        continue

                    seat_info = {"carriage": c_num, "seat": s_num}

                    if s_code == 2:
                        current_seat = seat_info
                    elif s_code == 0 and s_num in pref_list and c_num in allowed_carriages:
                        free_seats.append(seat_info)

        if not current_seat:
            return {"status": "unchanged", "message": "Could not identify current seat in map."}

        def get_seat_score(c_num_val: int, s_num_val: int) -> int:
            if s_num_val not in pref_list or c_num_val not in allowed_carriages:
                return -9999
            score = c_num_val * 100
            if s_num_val in tier_1:
                score += 150
            score -= pref_list.index(s_num_val)
            return score

        curr_score = get_seat_score(current_seat["carriage"], current_seat["seat"])

        if not free_seats:
            return {
                "status": "kept",
                "seat": current_seat,
                "message": f"No preferred free seats in carriages 21-25. Keeping initial Car {current_seat['carriage']}, Seat {current_seat['seat']}.",
            }

        free_seats.sort(key=lambda x: get_seat_score(x["carriage"], x["seat"]), reverse=True)
        best_seat = free_seats[0]
        best_score = get_seat_score(best_seat["carriage"], best_seat["seat"])

        if best_score <= curr_score:
            return {
                "status": "optimal",
                "seat": current_seat,
                "message": f"Current seat Car {current_seat['carriage']}, Seat {current_seat['seat']} is already optimal.",
            }

        url_put = f"https://api-gateway.cp.pt/cp/services/ticketing-api/train-seats/{sale_id}"
        seat_payload = {
            "originalSeats": [{
                "carriageNumber": current_seat["carriage"],
                "seatNumber": current_seat["seat"],
                "trainNumber": int(train_num),
            }],
            "requestedSeats": [{
                "carriageNumber": best_seat["carriage"],
                "seatNumber": best_seat["seat"],
                "trainNumber": int(train_num),
            }],
        }

        for attempt in range(2):
            try:
                response = self.session.put(url_put, json=seat_payload, timeout=15)
                response.raise_for_status()
                return {
                    "status": "changed",
                    "seat": best_seat,
                    "message": f"Upgraded seat to Car {best_seat['carriage']}, Seat {best_seat['seat']}!",
                }
            except Exception as e:
                time.sleep(1)

        return {
            "status": "failed_change",
            "seat": current_seat,
            "message": f"Seat change attempts failed. Retained Car {current_seat['carriage']}, Seat {current_seat['seat']}.",
        }

    def set_passenger_info(self, sale_id: str):
        """Attaches passenger Citizen Card (CC) details."""
        url = f"https://api-gateway.cp.pt/cp/services/ticketing-api/sale/{sale_id}/passengers"
        payload = {
            "salePassengers": [{
                "idtype": {"code": "CC", "designation": "Cartão de Cidadão"},
                "passengerID": self.passenger_id,
                "passengerName": self.name,
            }]
        }
        self.session.put(url, json=payload, timeout=15).raise_for_status()

    def apply_discount(self, sale_id: str):
        """Applies Passe Ferroviário Verde (Green Rail Pass, code 302)."""
        url = f"https://api-gateway.cp.pt/cp/services/ticketing-api/sale/{sale_id}/items"
        payload = {
            "requestedItems": [{
                "itemCode": "302", # Passe Ferroviário Verde
                "relatedTrain": None,
                "ticketIndex": 0,
                "type": "DISCOUNT",
                "inputData": self.green_pass_number,
            }]
        }
        self.session.put(url, json=payload, timeout=15).raise_for_status()

    def attach_client_info(self, sale_id: str):
        """Attaches client contact info."""
        url = f"https://api-gateway.cp.pt/cp/services/ticketing-api/sale/{sale_id}/client"
        payload = {
            "clientEmail": self.email,
            "clientID": self.email,
            "clientMobile": self.mobile,
            "clientName": self.name,
        }
        self.session.put(url, json=payload, timeout=15).raise_for_status()

    def confirm_purchase(self, sale_id: str) -> str:
        """Confirms the purchase and extracts the ticket PDF URL."""
        url = f"https://api-gateway.cp.pt/cp/services/ticketing-api/sale/{sale_id}/confirm"
        response = self.session.put(url, headers={"Content-Length": "0"}, timeout=20)
        response.raise_for_status()

        data = response.json()
        if data.get("status", {}).get("code") == "CONFIRMED":
            ticket_msg = "Check your email or CP App for your ticket."
            ticket_data = data.get("ticketData")
            if ticket_data and isinstance(ticket_data, list) and len(ticket_data) > 0:
                extracted_url = ticket_data[0].get("ticketURL")
                if extracted_url:
                    ticket_msg = f"https://cp.pt{extracted_url}"
            return ticket_msg
        else:
            raise Exception(f"Purchase not confirmed by CP. Status code: {data.get('status', {}).get('code')}")

    # --- ASYNC HIGH-LEVEL BOOKING PIPELINE ---

    async def execute_booking(self, payload: dict, status_callback=None) -> str:
        """
        Executes the full automated booking pipeline asynchronously.
        Supports status_callback(text) for real-time progress reporting.
        """
        async def notify(msg: str):
            print(f"[CP] {msg}")
            if status_callback:
                try:
                    await status_callback(msg)
                except Exception as e:
                    print(f"[!] Notification callback error: {e}")

        train_num = int(payload["train_number"])
        train_date = payload["train_date"]
        unlock_time_str = payload["unlock_time"]
        dep_station = payload["departure_station"]
        arr_station = payload["arrival_station"]

        await notify(f"🚂 Starting automated booking for Train **{train_num}** on **{train_date}**...")

        # 1. Fetch CP frontend config keys
        await notify("🔑 1/7 Fetching CP session configuration...")
        await asyncio.to_thread(self.fetch_fe_config)

        # 2. Resolve station codes if not already provided
        dep_code = payload.get("dep_code") or await asyncio.to_thread(self.get_station_code, dep_station)
        arr_code = payload.get("arr_code") or await asyncio.to_thread(self.get_station_code, arr_station)

        if not dep_code or not arr_code:
            raise ValueError(f"Could not resolve station codes for '{dep_station}' or '{arr_station}'.")

        # 3. Authenticate with Keycloak
        await notify("🔐 2/7 Logging into CP account...")
        await asyncio.to_thread(self.login)

        # 4. Lock seat (15 min hold)
        await notify(f"💺 3/7 Reserving seat on Train {train_num} (locking for 15 minutes)...")
        sale_id = await asyncio.to_thread(self.create_sale, train_num, train_date, dep_code, arr_code)

        # 5. Seat optimization
        seat_result = await asyncio.to_thread(self.change_seat, sale_id, train_num)
        await notify(f"🎯 4/7 Seat assignment: {seat_result.get('message', 'Checked')}")

        # 6. Wait for 24h unlock time
        departure_dt = datetime.strptime(f"{train_date} {unlock_time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=LISBON_TZ)
        unlock_time = departure_dt - timedelta(hours=24)
        now = datetime.now(LISBON_TZ)

        if now < unlock_time:
            wait_seconds = (unlock_time - now).total_seconds() + 2
            await notify(
                f"⏳ 5/7 Seat is safely held! Pausing for **{int(wait_seconds)}s** until 24h mark "
                f"({unlock_time.strftime('%H:%M:%S')} Lisbon time)..."
            )
            await asyncio.sleep(wait_seconds)
            await notify("⏰ Unlock mark reached! Waking up to finalize purchase...")
        else:
            await notify("⚡ 24h unlock mark has already passed! Proceeding immediately...")

        # 7. Refresh token and complete purchase
        await notify("🔄 6/7 Refreshing session authorization token...")
        await asyncio.to_thread(self.refresh_auth_token)

        await notify("📝 7/7 Attaching passenger ID, Green Pass discount, and client info...")
        await asyncio.to_thread(self.set_passenger_info, sale_id)
        await asyncio.to_thread(self.apply_discount, sale_id)
        await asyncio.to_thread(self.attach_client_info, sale_id)

        ticket_url = await asyncio.to_thread(self.confirm_purchase, sale_id)
        return ticket_url
