import os
import re
import asyncio
import traceback
from datetime import datetime, timedelta
import discord
from discord.ext import commands
from discord import ui
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from config import load_config
from cp import (
    CPClient, Booking, LISBON_TZ, SEAT_FREE, SEAT_CURRENT, TABLE_PLACE_TYPES,
    BookingAborted, NoSeatAvailable, PurchaseUncertain,
    find_current_seat, free_seats, seat_details,
)

# Load secrets from .env (local) or Google Secret Manager (VM)
load_config()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DEFAULT_USER_PING = os.getenv("DISCORD_USER_PING", "")
DEFAULT_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
DATA_DIR = os.getenv("COMBOIO_DATA_DIR", ".")

DEFAULT_WATCH_INTERVAL_MINUTES = 15
# Run a job that is late (bot restarting, event loop busy) instead of silently dropping it.
MISFIRE_GRACE_SECONDS = 600


def _allowed_user_ids() -> set[int]:
    """DISCORD_ALLOWED_USER_IDS (comma separated), falling back to the user in DISCORD_USER_PING."""
    explicit = os.getenv("DISCORD_ALLOWED_USER_IDS")
    if explicit:
        return {int(uid) for uid in re.findall(r"\d+", explicit)}
    match = re.fullmatch(r"\s*<@!?(\d+)>\s*", DEFAULT_USER_PING)
    return {int(match.group(1))} if match else set()


ALLOWED_USER_IDS = _allowed_user_ids()

# Shared client instance for lookups and UI queries
cp_lookup_client = CPClient()

# Bookings currently running (job_id -> Booking), for the seat picker and !list
ACTIVE_BOOKINGS: dict[str, Booking] = {}
# Live status message and counters of each watch job (in memory; recreated after a restart)
WATCH_STATUS: dict[str, dict] = {}


def is_allowed(user: discord.abc.User) -> bool:
    return not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS


def unlock_mark(payload: dict) -> datetime:
    """24h before the train leaves its first station: when Green Pass reservations open."""
    origin = datetime.strptime(f"{payload['train_date']} {payload['unlock_time']}", "%Y-%m-%d %H:%M:%S")
    return origin.replace(tzinfo=LISBON_TZ) - timedelta(hours=24)


def trip_departure(payload: dict) -> datetime:
    """Departure from the passenger's station (falls back to the origin departure for older jobs)."""
    time_str = payload.get("departure_time") or payload["unlock_time"]
    return datetime.strptime(f"{payload['train_date']} {time_str}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=LISBON_TZ)


# --- DISCORD BOT SETUP ---
class TicketBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        os.makedirs(DATA_DIR, exist_ok=True)
        jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{os.path.join(DATA_DIR, 'jobs.sqlite')}")}
        job_defaults = {"misfire_grace_time": MISFIRE_GRACE_SECONDS, "coalesce": True, "max_instances": 1}
        self.scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults, timezone=LISBON_TZ)

    async def warm_cache(self):
        """Pre-warms the CP stations list on startup so !ticket responds instantly."""
        try:
            print("[Bot] Pre-warming CP stations cache...")
            await asyncio.to_thread(cp_lookup_client.get_stations)
            print(f"[Bot] Stations cache warmed ({len(cp_lookup_client.stations_cache)} stations loaded)!")
        except Exception as e:
            print(f"[Bot] Warning: Could not pre-warm stations cache: {e}")

    async def setup_hook(self):
        # Jobs saved by older versions kept APScheduler's 1-second misfire grace; upgrade them before they can fire.
        self.scheduler.start(paused=True)
        for job in self.scheduler.get_jobs():
            if job.misfire_grace_time is not None and job.misfire_grace_time < MISFIRE_GRACE_SECONDS:
                job.modify(misfire_grace_time=MISFIRE_GRACE_SECONDS)
        self.scheduler.resume()
        print("✅ Resilient SQLite-backed Scheduler started (Lisbon Timezone)!")

        if not ALLOWED_USER_IDS:
            print("[Bot] ⚠️ DISCORD_ALLOWED_USER_IDS is not set: anyone in the server can use this bot!")
        asyncio.create_task(self.warm_cache())


intents = discord.Intents.default()
intents.message_content = True
bot = TicketBot(command_prefix="!", intents=intents)


@bot.check
async def only_allowed_users(ctx) -> bool:
    return is_allowed(ctx.author)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        return await ctx.send("🚫 You are not allowed to use this bot.")
    traceback.print_exception(type(error), error, error.__traceback__)
    await ctx.send(f"❌ **Error:** {error}")


async def resolve_channel(payload: dict):
    channel_id = payload.get("channel_id") or (int(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None)
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if not channel:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            print(f"[Bot] Could not fetch channel {channel_id}: {e}")
    return channel


class ProgressReporter:
    """A single Discord message that is edited in place as the booking progresses."""

    def __init__(self, channel, title: str, payload: dict):
        self.channel = channel
        self.title = title
        self.payload = payload
        self.lines: list[tuple[str | None, str]] = []  # (key, text)
        self.color = discord.Color.gold()
        self.message = None

    def embed(self) -> discord.Embed:
        # Show recent steps in description (capped to avoid Discord embed limits)
        desc = "\n".join(f"• {text}" for _, text in self.lines[-12:])
        embed = discord.Embed(title=self.title, description=desc[-4000:], color=self.color)
        embed.add_field(name="Date", value=self.payload["train_date"], inline=True)
        embed.add_field(name="Route", value=f"{self.payload['departure_station']} ➡️ {self.payload['arrival_station']}", inline=True)
        embed.set_footer(text="Live status • This message updates automatically")
        return embed

    async def start(self, content: str, view: ui.View = None):
        if not self.channel:
            return
        try:
            self.message = await self.channel.send(content=content, embed=self.embed(), view=view)
        except Exception as e:
            print(f"[Bot] Error sending progress message: {e}")

    async def update(self, text: str, key: str = None):
        # Consecutive lines with the same key (e.g. retry counters) replace each other instead of piling up.
        if key and self.lines and self.lines[-1][0] == key:
            self.lines[-1] = (key, text)
        else:
            self.lines.append((key, text))
        await self._edit()

    async def finish(self, text: str, color: discord.Color):
        self.lines.append((None, text))
        self.color = color
        await self._edit(view=None)

    async def _edit(self, **kwargs):
        if self.message:
            try:
                await self.message.edit(embed=self.embed(), **kwargs)
            except Exception as e:
                print(f"[Bot] Error editing progress message: {e}")


async def run_booking(job_id: str, payload: dict, booking: Booking, reporter: ProgressReporter, user_ping: str) -> tuple[str, str]:
    """
    Runs a Booking and posts its outcome next to the progress message.
    Returns (outcome, detail) with outcome in: confirmed, no_seat, uncertain, aborted, failed.
    Watch probes that never got a seat lock post nothing: the watch status message reports them instead.
    """
    ACTIVE_BOOKINGS[job_id] = booking
    try:
        ticket_result = await booking.run()
        outcome, detail = "confirmed", ticket_result
    except NoSeatAvailable as e:
        outcome, detail = "no_seat", str(e)
    except PurchaseUncertain as e:
        outcome, detail = "uncertain", str(e)
    except BookingAborted as e:
        outcome, detail = "aborted", str(e)
    except Exception as e:
        traceback.print_exception(type(e), e, e.__traceback__)
        outcome, detail = "failed", f"Booking failed: {e}"
    finally:
        ACTIVE_BOOKINGS.pop(job_id, None)

    print(f"[Bot] {job_id}: {outcome} — {detail}")
    if booking.mode == "watch" and reporter.message is None:
        return outcome, detail

    channel = reporter.channel
    train = payload["train_number"]
    if outcome == "confirmed":
        await reporter.finish("✅ **Automation completed successfully!**", discord.Color.green())
        embed = discord.Embed(
            title="🎉 Ticket Purchase Completed!",
            description=f"**Train:** {train}\n"
                        f"**Date:** {payload['train_date']}\n"
                        f"**Route:** {payload['departure_station']} ➡️ {payload['arrival_station']}",
            color=discord.Color.green(),
        )
        if booking.current_seat:
            embed.add_field(name="Seat", value=f"Car {booking.current_seat['carriage']}, Seat {booking.current_seat['seat']}", inline=False)
        embed.add_field(name="Ticket / PDF Link", value=detail, inline=False)
        embed.set_footer(text=f"Job: {job_id}")
        if channel:
            await channel.send(content=f"{user_ping} 🎫 **Success! Your ticket is confirmed.**", embed=embed)

    elif outcome == "no_seat":
        await reporter.finish(f"❌ **{detail}**", discord.Color.red())
        if channel:
            await channel.send(
                content=f"❌ {user_ping} **No seat could be locked on Train {train}!**\n`{detail}`",
                view=WatchOfferView(payload) if booking.mode == "scheduled" else None,
            )

    elif outcome == "uncertain":
        await reporter.finish(f"⚠️ **{detail}**", discord.Color.orange())
        if channel:
            await channel.send(content=f"⚠️ {user_ping} **Purchase status unknown for Train {train}!**\n`{detail}`")

    elif outcome == "aborted":
        await reporter.finish("🛑 **Aborted from Discord.**", discord.Color.dark_grey())

    else:
        await reporter.finish(f"❌ **{detail}**", discord.Color.red())
        if channel:
            await channel.send(content=f"❌ {user_ping} **Booking Failed for Train {train}!**\n`{detail}`")

    return outcome, detail


# --- TOP-LEVEL SCHEDULER CALLBACKS ---
# APScheduler stores these by reference (__main__:name) in jobs.sqlite: keep their names and signatures.

async def trigger_ticket_buy(job_id: str, payload: dict):
    """
    Called by APScheduler when a booking trigger time is reached.
    Sends only 2 messages with pings:
      1. A progress message that edits in-place as steps complete (with seat picker / abort buttons).
      2. A final result message with the ticket PDF or error details.
    """
    channel = await resolve_channel(payload)
    user_ping = payload.get("user_ping") or DEFAULT_USER_PING

    reporter = ProgressReporter(channel, f"🚆 Booking in Progress — Train {payload['train_number']}", payload)
    booking = Booking(payload, notify=reporter.update)
    # MESSAGE 1: Initial progress message with 1 ping
    await reporter.start(content=f"🤖 🔔 {user_ping} **Automation Trigger Fired!**", view=BookingControlView(booking))
    # MESSAGE 2 is posted by run_booking
    await run_booking(job_id, payload, booking, reporter, user_ping)


async def trigger_watch_check(job_id: str, payload: dict):
    """
    Called every N minutes for a watched train: tries to lock a seat and, if CP gives one, buys it.
    Checks that find nothing only edit the watch status message; a found seat pings like a normal booking.
    """
    if job_id in ACTIVE_BOOKINGS:
        return  # previous check is still checking out

    channel = await resolve_channel(payload)
    user_ping = payload.get("user_ping") or DEFAULT_USER_PING
    status = WATCH_STATUS.setdefault(job_id, {"message": None, "checks": 0, "last": None})
    status["checks"] += 1

    train = payload["train_number"]
    reporter = ProgressReporter(channel, f"🚆 Seat Found — Booking Train {train}", payload)

    async def notify(text: str, key: str = None):
        # Stay silent until CP actually hands us a seat.
        if reporter.message is None and booking.sale_id is not None:
            await reporter.start(
                content=f"🔔 {user_ping} **A seat opened up on watched Train {train}!** Booking it now...",
                view=BookingControlView(booking),
            )
        await reporter.update(text, key)

    booking = Booking(payload, notify=notify, mode="watch")
    outcome, detail = await run_booking(job_id, payload, booking, reporter, user_ping)

    job = bot.scheduler.get_job(job_id)
    checked_at = datetime.now(LISBON_TZ).strftime("%H:%M")
    if outcome in ("confirmed", "uncertain", "aborted"):
        # Stop either way: never risk buying the same trip twice.
        if job:
            job.remove()
        status["last"] = {
            "confirmed": f"{checked_at} — ✅ Ticket bought!",
            "uncertain": f"{checked_at} — ⚠️ Purchase status unknown, watch stopped.",
            "aborted": f"{checked_at} — 🛑 Aborted, watch stopped.",
        }[outcome]
        await update_watch_message(job_id, payload, next_run=None, color=discord.Color.green() if outcome == "confirmed" else discord.Color.dark_grey())
        WATCH_STATUS.pop(job_id, None)
        return

    reason = "no seat available" if outcome == "no_seat" else "error"
    status["last"] = f"{checked_at} — {reason}: {detail[:300]}"
    if job is None:
        # end_date reached (or the watch was cancelled during this check)
        await update_watch_message(job_id, payload, next_run=None, color=discord.Color.dark_grey())
        WATCH_STATUS.pop(job_id, None)
        if channel:
            await channel.send(f"⌛ {user_ping} Watch for Train {train} on {payload['train_date']} ended without getting a ticket.")
    else:
        await update_watch_message(job_id, payload, next_run=job.next_run_time)


def make_watch_embed(job_id: str, payload: dict, next_run: datetime | None, color=discord.Color.blue()) -> discord.Embed:
    status = WATCH_STATUS.get(job_id, {})
    embed = discord.Embed(title=f"👀 Watching Train {payload['train_number']} for a free seat", color=color)
    embed.add_field(name="Date", value=payload["train_date"], inline=True)
    embed.add_field(name="Route", value=f"{payload['departure_station']} ➡️ {payload['arrival_station']}", inline=True)
    embed.add_field(name="Every", value=f"{payload['watch_interval']} min", inline=True)
    embed.add_field(name="Checks so far", value=str(status.get("checks", 0)), inline=True)
    embed.add_field(name="Next check", value=next_run.strftime("%Y-%m-%d %H:%M") if next_run else "—", inline=True)
    embed.add_field(name="Stops at", value=payload["watch_until"], inline=True)
    embed.add_field(name="Last result", value=status.get("last") or "—", inline=False)
    embed.set_footer(text=f"Job ID: {job_id} • Cancel with !cancel")
    return embed


async def update_watch_message(job_id: str, payload: dict, next_run: datetime | None, color=discord.Color.blue()):
    status = WATCH_STATUS.setdefault(job_id, {"message": None, "checks": 0, "last": None})
    embed = make_watch_embed(job_id, payload, next_run, color)
    if status["message"]:
        try:
            await status["message"].edit(embed=embed)
            return
        except Exception as e:
            print(f"[Bot] Could not edit watch status message: {e}")
    channel = await resolve_channel(payload)
    if channel:
        try:
            status["message"] = await channel.send(embed=embed)
        except Exception as e:
            print(f"[Bot] Could not send watch status message: {e}")


def schedule_watch(payload: dict, interval_minutes: int, until: datetime) -> tuple[str, dict, datetime]:
    """Schedules periodic purchase attempts from the unlock mark (or now) until `until`."""
    # Green Pass checkout only works after the 24h unlock mark, so there's no point probing earlier.
    start = max(datetime.now(LISBON_TZ) + timedelta(seconds=5), unlock_mark(payload))
    if until <= start:
        raise ValueError(f"The watch would stop ({until:%Y-%m-%d %H:%M}) before it can start ({start:%Y-%m-%d %H:%M}).")

    job_id = f"watch_{payload['train_number']}_{payload['train_date']}"
    payload = {**payload, "watch_interval": interval_minutes, "watch_until": until.strftime("%Y-%m-%d %H:%M:%S")}
    bot.scheduler.add_job(
        trigger_watch_check,
        "interval",
        minutes=interval_minutes,
        start_date=start,
        end_date=until,
        next_run_time=start,  # without this the first check would only happen one interval after start
        args=[job_id, payload],
        id=job_id,
        replace_existing=True,
    )
    WATCH_STATUS[job_id] = {"message": None, "checks": 0, "last": None}
    return job_id, payload, start


async def reply_with_watch_status(interaction: discord.Interaction, job_id: str, payload: dict, start: datetime):
    content = f"{interaction.user.mention} 👀 Watch scheduled! First check at **{start:%Y-%m-%d %H:%M}** (Lisbon)."
    await interaction.response.send_message(content=content, embed=make_watch_embed(job_id, payload, start))
    original = await interaction.original_response()
    # A partial message is edited with the bot token, so it keeps working after the 15-min interaction token expires.
    status = WATCH_STATUS.setdefault(job_id, {"message": None, "checks": 0, "last": None})
    status["message"] = interaction.channel.get_partial_message(original.id)


# --- UI COMPONENTS ---
class GuardedView(ui.View):
    """A view only the allowed users can interact with."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_allowed(interaction.user):
            return True
        await interaction.response.send_message("🚫 You are not allowed to use this bot.", ephemeral=True)
        return False


def render_carriage(carriage: dict) -> str:
    """Text seat map of one carriage: `123` free, `·` taken, `[123]` your seat, `═` table, blank = aisle/gap."""
    rows = carriage["rows"]
    width = max((len(r) for r in rows), default=0)
    if width > len(rows):
        # CP's rows run along the car; transpose so each line is a cross-section (narrow enough for mobile).
        rows = [[row[i] if i < len(row) else None for row in rows] for i in range(width)]

    def cell(place):
        if place and place["seat"] is None and place["type"] in TABLE_PLACE_TYPES:
            return "  ═  "
        if not place or place["seat"] is None:
            return "     "
        if place["status"] == SEAT_CURRENT:
            return f"[{place['seat']:>3}]"
        if place["status"] == SEAT_FREE:
            return f" {place['seat']:>3} "
        return "   · "

    return "\n".join("".join(cell(p) for p in row).rstrip() for row in rows) or "(empty)"


class SeatPickerView(GuardedView):
    """Ephemeral seat map of a locked booking with selects to move to any free seat."""

    MAX_SEAT_SELECTS = 3  # rows 1-3; row 0 is the car select, row 4 the buttons

    def __init__(self, booking: Booking, carriages: list[dict]):
        super().__init__(timeout=900)
        self.booking = booking
        self.carriages = carriages
        current = find_current_seat(carriages)
        if current:
            self.carriage_number = current["carriage"]
        else:
            self.carriage_number = carriages[0]["number"] if carriages else None
        self.build()

    def build(self):
        self.clear_items()
        current = find_current_seat(self.carriages)

        if self.carriages:
            car_options = []
            for c in self.carriages[:25]:
                description = f"{len(free_seats([c]))} free seats"
                if current and current["carriage"] == c["number"]:
                    description += " · your seat"
                car_options.append(discord.SelectOption(
                    label=f"Car {c['number']}", value=str(c["number"]), description=description,
                    default=c["number"] == self.carriage_number,
                ))
            car_select = ui.Select(placeholder="Choose a car...", options=car_options, row=0)
            car_select.callback = self.on_car
            self.add_item(car_select)

        details = seat_details(self.carriages)

        def describe(seat: dict) -> str:
            place = details[(seat["carriage"], seat["seat"])]
            return ("Window" if place["window"] else "Aisle") + (" · table" if place["table"] else "")

        seats = sorted(free_seats(self.carriages, self.carriage_number), key=lambda s: s["seat"])
        for i in range(0, min(len(seats), 25 * self.MAX_SEAT_SELECTS), 25):
            chunk = seats[i:i + 25]
            seat_select = ui.Select(
                placeholder=f"Move to seat {chunk[0]['seat']}–{chunk[-1]['seat']}...",
                options=[
                    discord.SelectOption(label=f"Seat {s['seat']}", value=str(s["seat"]), description=describe(s))
                    for s in chunk
                ],
                row=1 + i // 25,
            )
            seat_select.callback = self.on_seat
            self.add_item(seat_select)

        refresh_btn = ui.Button(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary, row=4)
        refresh_btn.callback = self.on_refresh
        self.add_item(refresh_btn)

        manual_btn = ui.Button(label="Enter car / seat", emoji="⌨️", style=discord.ButtonStyle.secondary, row=4)
        manual_btn.callback = self.on_manual
        self.add_item(manual_btn)

    def make_embed(self) -> discord.Embed:
        current = find_current_seat(self.carriages)
        carriage = next((c for c in self.carriages if c["number"] == self.carriage_number), None)

        seat_map = render_carriage(carriage) if carriage else "(no seat map returned by CP)"
        if len(seat_map) > 3700:
            seat_map = seat_map[:3700] + "\n…"
        legend = "` 123 ` free  ·  `  · ` taken  ·  `[123]` your seat  ·  `═` table"
        embed = discord.Embed(
            title=f"🪑 Train {self.booking.train_num} — Car {self.carriage_number}",
            description=f"```\n{seat_map}\n```\n{legend}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Your seat", value=f"Car {current['carriage']}, Seat {current['seat']}" if current else "Unknown", inline=True)
        if self.booking.lock_expires_at:
            embed.add_field(name="Seat held until", value=self.booking.lock_expires_at.strftime("%H:%M:%S"), inline=True)
        if not self.booking.can_change_seat:
            embed.add_field(name="⚠️", value="The seat can no longer be changed (confirming, or lock expired).", inline=False)
        free_count = len(free_seats(self.carriages, self.carriage_number))
        if free_count > 25 * self.MAX_SEAT_SELECTS:
            embed.set_footer(text=f"Only the first {25 * self.MAX_SEAT_SELECTS} of {free_count} free seats are listed: use ⌨️ for others.")
        return embed

    async def apply(self, interaction: discord.Interaction, action=None):
        """Runs an optional seat action, reloads the seat map and redraws this message."""
        await interaction.response.defer()
        note = None
        if action:
            try:
                note = await action()
            except Exception as e:
                note = f"❌ {e}"
        try:
            self.carriages = await self.booking.fetch_seat_map()
        except Exception as e:
            note = f"{note}\n" if note else ""
            note += f"❌ Could not refresh the seat map: {e}"
        self.build()
        await interaction.edit_original_response(content=note, embed=self.make_embed(), view=self)

    def move_action(self, carriage: int, seat: int):
        async def move():
            new_seat = await self.booking.move_to(carriage, seat)
            return f"✅ Moved to Car {new_seat['carriage']}, Seat {new_seat['seat']}."
        return move

    async def on_car(self, interaction: discord.Interaction):
        self.carriage_number = int(interaction.data["values"][0])
        self.build()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_seat(self, interaction: discord.Interaction):
        seat = int(interaction.data["values"][0])
        await self.apply(interaction, self.move_action(self.carriage_number, seat))

    async def on_refresh(self, interaction: discord.Interaction):
        await self.apply(interaction)

    async def on_manual(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ManualSeatModal(self))


class ManualSeatModal(ui.Modal, title="Move to a Specific Seat"):
    carriage = ui.TextInput(label="Car number", min_length=1, max_length=3)
    seat = ui.TextInput(label="Seat number", min_length=1, max_length=4)

    def __init__(self, picker: SeatPickerView):
        super().__init__()
        self.picker = picker
        self.carriage.default = str(picker.carriage_number or "")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            carriage, seat = int(self.carriage.value), int(self.seat.value)
        except ValueError:
            return await interaction.response.send_message("❌ Car and seat must be numbers.", ephemeral=True)
        self.picker.carriage_number = carriage
        await self.picker.apply(interaction, self.picker.move_action(carriage, seat))


class BookingControlView(GuardedView):
    """Buttons on the live progress message of a running booking."""

    def __init__(self, booking: Booking):
        super().__init__(timeout=None)
        self.booking = booking

    @ui.button(label="View / change seat", emoji="🪑", style=discord.ButtonStyle.primary)
    async def seats(self, interaction: discord.Interaction, button: ui.Button):
        if not self.booking.can_change_seat:
            return await interaction.response.send_message(
                "ℹ️ The seat map is available while a seat is locked and before confirmation starts.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            carriages = await self.booking.fetch_seat_map()
        except Exception as e:
            return await interaction.followup.send(f"❌ Could not load the seat map: {e}", ephemeral=True)
        picker = SeatPickerView(self.booking, carriages)
        await interaction.followup.send(embed=picker.make_embed(), view=picker, ephemeral=True)

    @ui.button(label="Abort", emoji="🛑", style=discord.ButtonStyle.danger)
    async def abort(self, interaction: discord.Interaction, button: ui.Button):
        self.booking.abort()
        await interaction.response.send_message(
            "🛑 Aborting... CP releases the locked seat when its 15-minute hold expires.", ephemeral=True
        )


class WatchOfferView(GuardedView):
    """Offered when a scheduled booking couldn't lock a seat."""

    def __init__(self, payload: dict):
        super().__init__(timeout=3600)
        self.payload = payload

    @ui.button(label=f"Watch this train (every {DEFAULT_WATCH_INTERVAL_MINUTES} min)", emoji="👀", style=discord.ButtonStyle.primary)
    async def watch(self, interaction: discord.Interaction, button: ui.Button):
        payload = {**self.payload, "channel_id": interaction.channel_id, "user_ping": interaction.user.mention}
        try:
            job_id, payload, start = schedule_watch(payload, DEFAULT_WATCH_INTERVAL_MINUTES, trip_departure(payload))
        except ValueError as e:
            return await interaction.response.send_message(f"❌ **Error:** {e}", ephemeral=True)
        button.disabled = True
        await interaction.message.edit(view=self)
        await reply_with_watch_status(interaction, job_id, payload, start)


def build_trip_payload(interaction: discord.Interaction, route: dict, train_num: str, train_date: str, unlock_time: str) -> dict:
    """Validates the common modal fields and builds the job payload. Raises ValueError with a user-facing message."""
    try:
        train_num_int = int(train_num)
    except ValueError:
        raise ValueError("Train number must be numbers only.")
    try:
        datetime.strptime(f"{train_date} {unlock_time}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise ValueError("Train date must be YYYY-MM-DD and the unlock time HH:MM:SS.")

    return {
        "train_number": train_num_int,
        "train_date": train_date,
        "unlock_time": unlock_time,
        "departure_time": route["departure_time"],
        "departure_station": route["dep"],
        "arrival_station": route["arr"],
        "dep_code": route["dep_code"],
        "arr_code": route["arr_code"],
        "channel_id": interaction.channel_id,
        "user_ping": interaction.user.mention,
    }


class DetailModal(ui.Modal, title="Review & Schedule Trip"):
    train_num = ui.TextInput(label="Train Number", min_length=1, max_length=5)
    train_date = ui.TextInput(label="Train Ride Date (YYYY-MM-DD)", min_length=10, max_length=10)
    unlock_time = ui.TextInput(label="Unlock Time / Dep. from 1st Station", min_length=8, max_length=8)
    trigger_datetime = ui.TextInput(label="Trigger Datetime (YYYY-MM-DD HH:MM:SS)", min_length=19, max_length=19)

    def __init__(self, route: dict, def_train: str = "", def_date: str = "", def_unlock: str = "", def_trigger: str = ""):
        super().__init__()
        self.route = route
        self.train_num.default = def_train
        self.train_date.default = def_date
        self.unlock_time.default = def_unlock
        self.trigger_datetime.default = def_trigger

    async def on_submit(self, interaction: discord.Interaction):
        try:
            payload = build_trip_payload(interaction, self.route, self.train_num.value, self.train_date.value, self.unlock_time.value)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ **Error:** {e}", ephemeral=True)

        try:
            trigger_time = datetime.strptime(self.trigger_datetime.value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LISBON_TZ)
        except ValueError:
            return await interaction.response.send_message("❌ **Error:** Invalid Trigger Datetime format.", ephemeral=True)

        if trigger_time <= datetime.now(LISBON_TZ):
            return await interaction.response.send_message("❌ **Error:** Scheduled trigger time must be in the future!", ephemeral=True)

        job_id = f"job_{payload['train_number']}_{payload['train_date']}_{payload['unlock_time'].replace(':', '')}"

        bot.scheduler.add_job(
            trigger_ticket_buy,
            "date",
            run_date=trigger_time,
            args=[job_id, payload],
            id=job_id,
            replace_existing=True,
        )

        embed = discord.Embed(title="🎫 Ticket Booking Scheduled!", color=discord.Color.green())
        embed.add_field(name="Train Number", value=self.train_num.value, inline=True)
        embed.add_field(name="Train Ride Date", value=self.train_date.value, inline=True)
        embed.add_field(name="Unlock Time", value=self.unlock_time.value, inline=True)
        embed.add_field(name="From", value=self.route["dep"], inline=True)
        embed.add_field(name="To", value=self.route["arr"], inline=True)
        embed.add_field(
            name="Automation Trigger Datetime",
            value=f"⏰ {trigger_time.strftime('%Y-%m-%d %H:%M:%S')} (Lisbon)",
            inline=False,
        )
        embed.set_footer(text=f"Job ID: {job_id} • Status updates will be posted in this channel")
        await interaction.response.send_message(content=f"{interaction.user.mention} Booking confirmed!", embed=embed)


class WatchModal(ui.Modal, title="Watch Train for a Free Seat"):
    train_num = ui.TextInput(label="Train Number", min_length=1, max_length=5)
    train_date = ui.TextInput(label="Train Ride Date (YYYY-MM-DD)", min_length=10, max_length=10)
    unlock_time = ui.TextInput(label="Unlock Time / Dep. from 1st Station", min_length=8, max_length=8)
    interval = ui.TextInput(label="Check every N minutes", min_length=1, max_length=4)
    stop_at = ui.TextInput(label="Stop watching at (YYYY-MM-DD HH:MM:SS)", min_length=19, max_length=19)

    def __init__(self, route: dict, def_train: str = "", def_date: str = "", def_unlock: str = "", def_stop: str = ""):
        super().__init__()
        self.route = route
        self.train_num.default = def_train
        self.train_date.default = def_date
        self.unlock_time.default = def_unlock
        self.interval.default = str(DEFAULT_WATCH_INTERVAL_MINUTES)
        self.stop_at.default = def_stop

    async def on_submit(self, interaction: discord.Interaction):
        try:
            payload = build_trip_payload(interaction, self.route, self.train_num.value, self.train_date.value, self.unlock_time.value)
            try:
                interval = int(self.interval.value)
            except ValueError:
                raise ValueError("The interval must be a whole number of minutes.")
            if not 1 <= interval <= 1440:
                raise ValueError("The interval must be between 1 and 1440 minutes.")
            try:
                until = datetime.strptime(self.stop_at.value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LISBON_TZ)
            except ValueError:
                raise ValueError("Invalid stop datetime format.")
            job_id, payload, start = schedule_watch(payload, interval, until)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ **Error:** {e}", ephemeral=True)

        await reply_with_watch_status(interaction, job_id, payload, start)


class TrainSelectView(GuardedView):
    def __init__(self, journeys: list, route: dict, date_val: str, mode: str):
        super().__init__(timeout=None)
        self.journeys = journeys
        self.route = route
        self.date_val = date_val
        self.mode = mode  # "ticket" or "watch"
        self.selected_train_num = None
        self.origin_time_str = None
        self.departure_time_str = None

        options = []
        for j in journeys[:25]:  # Discord select menu limit
            try:
                t_num = j["travelSections"][0]["trainNumber"]
                dep_t = j["departureTime"]
                arr_t = j["arrivalTime"]
                options.append(discord.SelectOption(label=f"Train {t_num} ({dep_t} - {arr_t})", value=str(t_num)))
            except (KeyError, IndexError):
                continue

        self.select_menu = ui.Select(placeholder="Select a Train...", options=options, custom_id="select_train")
        self.select_menu.callback = self.train_select_callback
        self.add_item(self.select_menu)

        label = "Review & Watch" if mode == "watch" else "Review & Schedule"
        self.confirm_btn = ui.Button(label=label, disabled=True, style=discord.ButtonStyle.green, row=1)
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def train_select_callback(self, interaction: discord.Interaction):
        self.selected_train_num = interaction.data["values"][0]

        for opt in self.select_menu.options:
            if opt.value == self.selected_train_num:
                self.select_menu.placeholder = opt.label
                break

        await interaction.response.defer()

        # Departure time from the chosen station, per CP journey data
        self.departure_time_str = "00:00:00"
        for j in self.journeys:
            try:
                if str(j["travelSections"][0]["trainNumber"]) == str(self.selected_train_num):
                    dep_val = j.get("departureTime", "00:00")
                    self.departure_time_str = dep_val if len(dep_val) == 8 else f"{dep_val}:00"
                    break
            except (KeyError, IndexError):
                continue

        # Query IP API for exact train origin departure time
        origin_dt_str = await asyncio.to_thread(CPClient.get_origin_time, self.selected_train_num, self.date_val)
        if origin_dt_str:
            try:
                dt = datetime.strptime(origin_dt_str, "%d/%m/%Y %H:%M:%S")
                self.origin_time_str = dt.strftime("%H:%M:%S")
            except ValueError:
                self.origin_time_str = self.departure_time_str
        else:
            # If IP API fails or is unreachable, use station departure time as sensible default
            self.origin_time_str = self.departure_time_str

        self.confirm_btn.disabled = False
        self.confirm_btn.label = f"{'Review & Watch' if self.mode == 'watch' else 'Review & Schedule'} (Train {self.selected_train_num})"
        await interaction.edit_original_response(view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
        route = {**self.route, "departure_time": self.departure_time_str}

        if self.mode == "watch":
            modal = WatchModal(
                route,
                str(self.selected_train_num),
                self.date_val,
                self.origin_time_str,
                f"{self.date_val} {self.departure_time_str}",
            )
            return await interaction.response.send_modal(modal)

        dt_str = f"{self.date_val} {self.origin_time_str}"
        try:
            origin_dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            unlock_mark_dt = origin_dt - timedelta(hours=24)
            # Trigger 5 minutes prior to 24h unlock mark to reserve the seat
            trigger_dt = unlock_mark_dt - timedelta(minutes=5)
            trigger_str = trigger_dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            trigger_str = "YYYY-MM-DD HH:MM:SS"

        modal = DetailModal(
            route,
            str(self.selected_train_num),
            self.date_val,
            self.origin_time_str,
            trigger_str,
        )
        await interaction.response.send_modal(modal)


class BookingView(GuardedView):
    def __init__(self, stations: list, date_val: str, mode: str = "ticket"):
        super().__init__(timeout=None)
        self.stations = stations
        self.date_val = date_val
        self.mode = mode  # "ticket" or "watch"
        self.dep = "Lisboa Santa Apolonia"
        self.arr = "Entroncamento"
        self.update_components()

    def get_station_options(self, current_val: str):
        favs = ["Lisboa Santa Apolonia", "Entroncamento", "Lisboa Oriente"]
        display = [s for f in favs for s in self.stations if s.get("designation") == f]
        others = sorted([s for s in self.stations if s.get("designation") not in favs], key=lambda x: x.get("designation", ""))
        display.extend(others[: 25 - len(display)])

        return [
            discord.SelectOption(
                label=s["designation"],
                value=s["designation"],
                default=(s["designation"] == current_val),
            )
            for s in display
        ]

    def update_components(self):
        self.clear_items()
        dep_select = ui.Select(placeholder=f"FROM: {self.dep}", options=self.get_station_options(self.dep), custom_id="select_dep")
        dep_select.callback = self.select_dep_callback

        arr_select = ui.Select(placeholder=f"TO: {self.arr}", options=self.get_station_options(self.arr), custom_id="select_arr")
        arr_select.callback = self.select_arr_callback

        self.add_item(dep_select)
        self.add_item(arr_select)

        button = ui.Button(label="Search Available Trains ➡️", style=discord.ButtonStyle.primary, row=2)
        button.callback = self.search_callback
        self.add_item(button)

    def update_embed(self):
        title = "👀 Train Watch Setup" if self.mode == "watch" else "🚆 Train Ticket Command Center"
        embed = discord.Embed(
            title=title,
            description="Select your route, then click Search.",
            color=discord.Color.blue(),
        )
        embed.add_field(name="📅 DATE", value=f"**{self.date_val}**", inline=False)
        embed.add_field(name="📍 FROM", value=f"**{self.dep}**", inline=True)
        embed.add_field(name="🏁 TO", value=f"**{self.arr}**", inline=True)
        return embed

    async def select_dep_callback(self, interaction: discord.Interaction):
        self.dep = interaction.data["values"][0]
        self.update_components()
        await interaction.response.edit_message(embed=self.update_embed(), view=self)

    async def select_arr_callback(self, interaction: discord.Interaction):
        self.arr = interaction.data["values"][0]
        self.update_components()
        await interaction.response.edit_message(embed=self.update_embed(), view=self)

    async def search_callback(self, interaction: discord.Interaction):
        if self.dep == self.arr:
            return await interaction.response.send_message("❌ Departure and Arrival stations cannot be the same!", ephemeral=True)

        await interaction.response.defer()
        dep_code = next((s["code"] for s in self.stations if s["designation"] == self.dep), None)
        arr_code = next((s["code"] for s in self.stations if s["designation"] == self.arr), None)

        try:
            journeys = await asyncio.to_thread(cp_lookup_client.search_journeys, dep_code, arr_code, self.date_val)
        except Exception as e:
            return await interaction.followup.send(f"❌ API Error fetching journeys: {e}", ephemeral=True)

        if not journeys:
            return await interaction.followup.send("❌ No trains found for this route and date.", ephemeral=True)

        route = {"dep": self.dep, "arr": self.arr, "dep_code": dep_code, "arr_code": arr_code}
        view = TrainSelectView(journeys, route, self.date_val, self.mode)
        embed = discord.Embed(
            title="🚆 Select a Train",
            description=f"**Date:** {self.date_val}\n**Route:** {self.dep} ➡️ {self.arr}",
            color=discord.Color.blue(),
        )
        await interaction.edit_original_response(content="", embed=embed, view=view)


class CancelDropdown(ui.Select):
    def __init__(self, jobs):
        options = []
        for job in jobs[:25]:
            job_id, payload = job.args
            kind = "👀 Watch" if job_id.startswith("watch_") else "🎫 Booking"
            options.append(
                discord.SelectOption(
                    label=f"{kind}: Train {payload['train_number']} ({payload['train_date']})",
                    value=job_id,
                    description=f"From {payload['departure_station']} to {payload['arrival_station']}",
                )
            )
        super().__init__(placeholder="Select a booking or watch to cancel...", options=options)

    async def callback(self, interaction: discord.Interaction):
        job_id = self.values[0]
        try:
            bot.scheduler.remove_job(job_id)
            WATCH_STATUS.pop(job_id, None)
            await interaction.response.send_message(
                f"🗑️ {interaction.user.mention} **Cancelled!** Job `{job_id}` was removed.",
                ephemeral=False,
            )
        except Exception:
            await interaction.response.send_message("❌ **Error:** Could not cancel this job.", ephemeral=True)


class CancelView(GuardedView):
    def __init__(self, jobs):
        super().__init__()
        self.add_item(CancelDropdown(jobs))


# --- BOT COMMANDS ---
async def start_route_wizard(ctx, date_val: str | None, mode: str):
    if not date_val:
        tomorrow = (datetime.now(LISBON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        return await ctx.send(
            f"ℹ️ **Missing Date!** Usage: `!{mode} YYYY-MM-DD`\nExample for tomorrow: `!{mode} {tomorrow}`"
        )

    try:
        datetime.strptime(date_val, "%Y-%m-%d")
    except ValueError:
        return await ctx.send("❌ Invalid date format. Please use **YYYY-MM-DD**.")

    msg = await ctx.send("⏳ Loading CP station list...")
    try:
        stations = await asyncio.to_thread(cp_lookup_client.get_stations)
    except Exception as e:
        return await msg.edit(content=f"❌ Error loading stations: {e}")

    view = BookingView(stations, date_val, mode)
    await msg.edit(content="", embed=view.update_embed(), view=view)


@bot.command()
async def ticket(ctx, date_val: str = None):
    """Start train ticket scheduling workflow: !ticket YYYY-MM-DD"""
    await start_route_wizard(ctx, date_val, "ticket")


@bot.command()
async def watch(ctx, date_val: str = None):
    """Retry buying a (full) train every N minutes until a seat frees up: !watch YYYY-MM-DD"""
    await start_route_wizard(ctx, date_val, "watch")


@bot.command(name="list")
async def list_jobs(ctx):
    """List all scheduled ticket purchases, watches and bookings in progress."""
    jobs = bot.scheduler.get_jobs()
    if not jobs and not ACTIVE_BOOKINGS:
        return await ctx.send("ℹ️ **There are currently no active ticket purchases scheduled.**")

    embed = discord.Embed(title="📋 Active Scheduled Bookings", color=discord.Color.purple())
    for job_id, booking in ACTIVE_BOOKINGS.items():
        seat = booking.current_seat
        seat_text = f"Car {seat['carriage']}, Seat {seat['seat']}" if seat else "not locked yet"
        embed.add_field(
            name=f"⚙️ In progress: {job_id}",
            value=f"**Train:** {booking.train_num} on {booking.train_date}\n**State:** {booking.state} • **Seat:** {seat_text}",
            inline=False,
        )
    for job in jobs[: 25 - len(embed.fields)]:
        job_id, payload = job.args
        run_time = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "Unknown"
        route = f"**Route:** {payload['departure_station']} ➡️ {payload['arrival_station']}\n"
        if job_id.startswith("watch_"):
            details = (
                f"**Train Number:** {payload['train_number']} on {payload['train_date']}\n"
                f"{route}"
                f"👀 **Watching** every {payload['watch_interval']} min until {payload['watch_until']}\n"
                f"⏰ **Next check:** {run_time} (Lisbon)"
            )
            embed.add_field(name=f"👀 Watch ID: {job_id}", value=details, inline=False)
        else:
            details = (
                f"**Train Number:** {payload['train_number']}\n"
                f"**Train Ride Date:** {payload['train_date']}\n"
                f"**Unlock Time:** {payload['unlock_time']}\n"
                f"{route}"
                f"⏰ **Automation Triggers at:** {run_time} (Lisbon)"
            )
            embed.add_field(name=f"🎫 Job ID: {job_id}", value=details, inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def cancel(ctx):
    """Cancel a scheduled ticket purchase or watch."""
    jobs = bot.scheduler.get_jobs()
    if not jobs:
        return await ctx.send("ℹ️ **There are currently no active ticket purchases scheduled.**")
    embed = discord.Embed(
        title="🗑️ Cancel Active Bookings",
        description="Select a booking or watch to remove. Bookings already in progress are stopped with their 🛑 Abort button.",
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed, view=CancelView(jobs))


@bot.command()
async def ping(ctx):
    """Health check command."""
    jobs_count = len(bot.scheduler.get_jobs())
    await ctx.send(
        f"🏓 Pong! Bot latency: `{round(bot.latency * 1000)}ms`. "
        f"Scheduled jobs: `{jobs_count}`. In progress: `{len(ACTIVE_BOOKINGS)}`."
    )


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("Missing DISCORD_BOT_TOKEN (in .env or the Secret Manager secret).")
    bot.run(BOT_TOKEN)
