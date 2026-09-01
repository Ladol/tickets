import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import discord
from discord.ext import commands
from discord import ui
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from dotenv import load_dotenv

from cp import CPClient, LISBON_TZ

# Load secrets from .env
load_dotenv()
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DEFAULT_USER_PING = os.getenv("DISCORD_USER_PING", "")
DEFAULT_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

# Shared client instance for lookups and UI queries
cp_lookup_client = CPClient()


# --- DISCORD BOT SETUP ---
class TicketBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        jobstores = {"default": SQLAlchemyJobStore(url="sqlite:///jobs.sqlite")}
        self.scheduler = AsyncIOScheduler(jobstores=jobstores, timezone=LISBON_TZ)

    async def warm_cache(self):
        """Pre-warms the CP stations list on startup so !ticket responds instantly."""
        try:
            print("[Bot] Pre-warming CP stations cache...")
            await asyncio.to_thread(cp_lookup_client.get_stations)
            print(f"[Bot] Stations cache warmed ({len(cp_lookup_client.stations_cache)} stations loaded)!")
        except Exception as e:
            print(f"[Bot] Warning: Could not pre-warm stations cache: {e}")

    async def setup_hook(self):
        self.scheduler.start()
        print("✅ Resilient SQLite-backed Scheduler started (Lisbon Timezone)!")
        asyncio.create_task(self.warm_cache())


intents = discord.Intents.default()
intents.message_content = True
bot = TicketBot(command_prefix="!", intents=intents)


# --- TOP-LEVEL SCHEDULER CALLBACK ---
async def trigger_ticket_buy(job_id: str, payload: dict):
    """
    Called by APScheduler when a booking trigger time is reached.
    Directly executes the CP automated purchase in-process with live Discord updates.
    Sends only 2 messages with pings:
      1. An initial progress message that edits in-place as steps complete.
      2. A final result message with the ticket PDF or error details.
    """
    channel_id = payload.get("channel_id") or (int(DEFAULT_CHANNEL_ID) if DEFAULT_CHANNEL_ID else None)
    user_ping = payload.get("user_ping") or DEFAULT_USER_PING

    channel = None
    if channel_id:
        channel = bot.get_channel(channel_id)
        if not channel:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception as e:
                print(f"[Bot] Could not fetch channel {channel_id}: {e}")

    history = [f"• Starting automated booking for Train **{payload['train_number']}**..."]

    def make_progress_embed(color=discord.Color.gold()):
        # Show recent steps in description (capped to avoid Discord embed limits)
        desc = "\n".join(history[-10:])
        embed = discord.Embed(
            title=f"🚆 Booking in Progress — Train {payload['train_number']}",
            description=desc,
            color=color,
        )
        embed.add_field(name="Date", value=payload["train_date"], inline=True)
        embed.add_field(name="Route", value=f"{payload['departure_station']} ➡️ {payload['arrival_station']}", inline=True)
        embed.set_footer(text="Live status • This message updates automatically")
        return embed

    progress_msg = None
    if channel:
        try:
            # MESSAGE 1: Initial progress message with 1 ping
            progress_msg = await channel.send(
                content=f"🤖 🔔 {user_ping} **Automation Trigger Fired!**",
                embed=make_progress_embed(),
            )
        except Exception as e:
            print(f"[Bot] Error sending initial progress message: {e}")

    async def update_progress(text: str):
        print(f"[Bot] {text}")
        history.append(f"• {text}")
        if progress_msg:
            try:
                await progress_msg.edit(embed=make_progress_embed())
            except Exception as e:
                print(f"[Bot] Error editing progress message: {e}")

    try:
        # Create a dedicated, clean CPClient instance for this booking execution
        booking_client = CPClient()
        ticket_result = await booking_client.execute_booking(payload, status_callback=update_progress)

        # Update the progress message one last time in green
        history.append("✅ **Automation completed successfully!**")
        if progress_msg:
            try:
                await progress_msg.edit(embed=make_progress_embed(color=discord.Color.green()))
            except Exception:
                pass

        # MESSAGE 2: Final celebration message with 2nd ping
        embed = discord.Embed(
            title="🎉 Ticket Purchase Completed!",
            description=f"**Train:** {payload['train_number']}\n"
                        f"**Date:** {payload['train_date']}\n"
                        f"**Route:** {payload['departure_station']} ➡️ {payload['arrival_station']}",
            color=discord.Color.green(),
        )
        embed.add_field(name="Ticket / PDF Link", value=ticket_result, inline=False)
        embed.set_footer(text=f"Job: {job_id}")

        if channel:
            await channel.send(content=f"{user_ping} 🎫 **Success! Your ticket is confirmed.**", embed=embed)

    except Exception as e:
        error_msg = f"Booking failed: {e}"
        print(f"[Bot] {error_msg}")
        history.append(f"❌ **{error_msg}**")
        if progress_msg:
            try:
                await progress_msg.edit(embed=make_progress_embed(color=discord.Color.red()))
            except Exception:
                pass

        # MESSAGE 2 (Failure): Final failure alert with 2nd ping
        if channel:
            await channel.send(
                content=f"❌ {user_ping} **Booking Failed for Train {payload['train_number']}!**\n`{error_msg}`"
            )


# --- UI COMPONENTS ---
class DetailModal(ui.Modal, title="Review & Schedule Trip"):
    train_num = ui.TextInput(label="Train Number", min_length=1, max_length=5)
    train_date = ui.TextInput(label="Train Ride Date (YYYY-MM-DD)", min_length=10, max_length=10)
    unlock_time = ui.TextInput(label="Unlock Time / Dep. from 1st Station", min_length=8, max_length=8)
    trigger_datetime = ui.TextInput(label="Trigger Datetime (YYYY-MM-DD HH:MM:SS)", min_length=19, max_length=19)

    def __init__(self, dep: str, arr: str, dep_code: str, arr_code: str,
                 def_train: str = "", def_date: str = "", def_unlock: str = "", def_trigger: str = ""):
        super().__init__()
        self.dep = dep
        self.arr = arr
        self.dep_code = dep_code
        self.arr_code = arr_code
        self.train_num.default = def_train
        self.train_date.default = def_date
        self.unlock_time.default = def_unlock
        self.trigger_datetime.default = def_trigger

    async def on_submit(self, interaction: discord.Interaction):
        try:
            train_num_int = int(self.train_num.value)
        except ValueError:
            return await interaction.response.send_message("❌ **Error:** Train number must be numbers only.", ephemeral=True)

        try:
            trigger_time = datetime.strptime(self.trigger_datetime.value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LISBON_TZ)
        except ValueError:
            return await interaction.response.send_message("❌ **Error:** Invalid Trigger Datetime format.", ephemeral=True)

        if trigger_time <= datetime.now(LISBON_TZ):
            return await interaction.response.send_message("❌ **Error:** Scheduled trigger time must be in the future!", ephemeral=True)

        payload = {
            "train_number": train_num_int,
            "train_date": self.train_date.value,
            "unlock_time": self.unlock_time.value,
            "departure_station": self.dep,
            "arrival_station": self.arr,
            "dep_code": self.dep_code,
            "arr_code": self.arr_code,
            "channel_id": interaction.channel_id,
            "user_ping": interaction.user.mention,
        }
        job_id = f"job_{train_num_int}_{self.train_date.value}_{self.unlock_time.value.replace(':', '')}"

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
        embed.add_field(name="From", value=self.dep, inline=True)
        embed.add_field(name="To", value=self.arr, inline=True)
        embed.add_field(
            name="Automation Trigger Datetime",
            value=f"⏰ {trigger_time.strftime('%Y-%m-%d %H:%M:%S')} (Lisbon)",
            inline=False,
        )
        embed.set_footer(text=f"Job ID: {job_id} • Status updates will be posted in this channel")
        await interaction.response.send_message(content=f"{interaction.user.mention} Booking confirmed!", embed=embed)


class TrainSelectView(ui.View):
    def __init__(self, journeys: list, dep: str, arr: str, dep_code: str, arr_code: str, date_val: str):
        super().__init__(timeout=None)
        self.journeys = journeys
        self.dep = dep
        self.arr = arr
        self.dep_code = dep_code
        self.arr_code = arr_code
        self.date_val = date_val
        self.selected_train_num = None
        self.origin_time_str = None

        options = []
        for j in journeys[:25]:  # Discord select menu limit
            try:
                t_num = j["travelSections"][0]["trainNumber"]
                dep_t = j["departureTime"]
                arr_t = j["arrivalTime"]
                options.append(discord.SelectOption(label=f"Train {t_num} ({dep_t} - {arr_t})", value=str(t_num)))
            except KeyError:
                continue

        self.select_menu = ui.Select(placeholder="Select a Train...", options=options, custom_id="select_train")
        self.select_menu.callback = self.train_select_callback
        self.add_item(self.select_menu)

        self.confirm_btn = ui.Button(label="Review & Schedule", disabled=True, style=discord.ButtonStyle.green, row=1)
        self.confirm_btn.callback = self.confirm_callback
        self.add_item(self.confirm_btn)

    async def train_select_callback(self, interaction: discord.Interaction):
        self.selected_train_num = interaction.data["values"][0]

        for opt in self.select_menu.options:
            if opt.value == self.selected_train_num:
                self.select_menu.placeholder = opt.label
                break

        await interaction.response.defer()

        # Determine fallback departure time from CP journey data
        fallback_time = "00:00:00"
        for j in self.journeys:
            try:
                if str(j["travelSections"][0]["trainNumber"]) == str(self.selected_train_num):
                    dep_val = j.get("departureTime", "00:00")
                    fallback_time = dep_val if len(dep_val) == 8 else f"{dep_val}:00"
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
                self.origin_time_str = fallback_time
        else:
            # If IP API fails or is unreachable, use station departure time as sensible default
            self.origin_time_str = fallback_time

        self.confirm_btn.disabled = False
        self.confirm_btn.label = f"Review & Schedule (Train {self.selected_train_num})"
        await interaction.edit_original_response(view=self)

    async def confirm_callback(self, interaction: discord.Interaction):
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
            self.dep,
            self.arr,
            self.dep_code,
            self.arr_code,
            str(self.selected_train_num),
            self.date_val,
            self.origin_time_str,
            trigger_str,
        )
        await interaction.response.send_modal(modal)


class BookingView(ui.View):
    def __init__(self, stations: list, date_val: str):
        super().__init__(timeout=None)
        self.stations = stations
        self.date_val = date_val
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
        embed = discord.Embed(
            title="🚆 Train Ticket Command Center",
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

        view = TrainSelectView(journeys, self.dep, self.arr, dep_code, arr_code, self.date_val)
        embed = discord.Embed(
            title="🚆 Select a Train",
            description=f"**Date:** {self.date_val}\n**Route:** {self.dep} ➡️ {self.arr}",
            color=discord.Color.blue(),
        )
        await interaction.edit_original_response(content="", embed=embed, view=view)


class CancelDropdown(ui.Select):
    def __init__(self, jobs):
        options = []
        for job in jobs:
            job_id, payload = job.args
            options.append(
                discord.SelectOption(
                    label=f"Train {payload['train_number']} ({payload['train_date']})",
                    value=job_id,
                    description=f"From {payload['departure_station']} to {payload['arrival_station']}",
                )
            )
        super().__init__(placeholder="Select a booking to cancel...", options=options)

    async def callback(self, interaction: discord.Interaction):
        job_id = self.values[0]
        try:
            bot.scheduler.remove_job(job_id)
            await interaction.response.send_message(
                f"🗑️ {interaction.user.mention} **Cancelled!** Job `{job_id}` was removed.",
                ephemeral=False,
            )
        except Exception:
            await interaction.response.send_message("❌ **Error:** Could not cancel this job.", ephemeral=True)


class CancelView(ui.View):
    def __init__(self, jobs):
        super().__init__()
        self.add_item(CancelDropdown(jobs))


# --- BOT COMMANDS ---
@bot.command()
async def ticket(ctx, date_val: str = None):
    """Start train ticket scheduling workflow: !ticket YYYY-MM-DD"""
    if not date_val:
        tomorrow = (datetime.now(LISBON_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        return await ctx.send(
            f"ℹ️ **Missing Date!** Usage: `!ticket YYYY-MM-DD`\nExample for tomorrow: `!ticket {tomorrow}`"
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

    view = BookingView(stations, date_val)
    await msg.edit(content="", embed=view.update_embed(), view=view)


@bot.command()
async def list(ctx):
    """List all scheduled ticket purchases."""
    jobs = bot.scheduler.get_jobs()
    if not jobs:
        return await ctx.send("ℹ️ **There are currently no active ticket purchases scheduled.**")

    embed = discord.Embed(title="📋 Active Scheduled Bookings", color=discord.Color.purple())
    for job in jobs:
        job_id, payload = job.args
        run_time = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "Unknown"
        details = (
            f"**Train Number:** {payload['train_number']}\n"
            f"**Train Ride Date:** {payload['train_date']}\n"
            f"**Unlock Time:** {payload['unlock_time']}\n"
            f"**Route:** {payload['departure_station']} ➡️ {payload['arrival_station']}\n"
            f"⏰ **Automation Triggers at:** {run_time} (Lisbon)"
        )
        embed.add_field(name=f"🎫 Job ID: {job_id}", value=details, inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def cancel(ctx):
    """Cancel a scheduled ticket purchase."""
    jobs = bot.scheduler.get_jobs()
    if not jobs:
        return await ctx.send("ℹ️ **There are currently no active ticket purchases scheduled.**")
    embed = discord.Embed(
        title="🗑️ Cancel Active Bookings",
        description="Select a booking to remove.",
        color=discord.Color.red(),
    )
    await ctx.send(embed=embed, view=CancelView(jobs))


@bot.command()
async def ping(ctx):
    """Health check command."""
    jobs_count = len(bot.scheduler.get_jobs())
    await ctx.send(f"🏓 Pong! Bot latency: `{round(bot.latency * 1000)}ms`. Scheduled jobs: `{jobs_count}`.")


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("Missing DISCORD_BOT_TOKEN in .env file.")
    bot.run(BOT_TOKEN)
