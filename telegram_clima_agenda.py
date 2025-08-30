#!/usr/bin/env python3
import os, requests, datetime as dt, asyncio
from dateutil import tz
from ics import Calendar
from pathlib import Path
from telegram import Bot

# --- cargar .env ---
def load_env(path):
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

HERE = Path(__file__).resolve().parent
load_env(HERE / ".env")

# --- CONFIG desde .env ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
CITY      = os.getenv("CITY", "Agua de Oro, Cordoba, Argentina")
TZ_NAME   = os.getenv("TZ_NAME", "America/Argentina/Cordoba")
# Permite múltiples iCal separados por coma. Filtra vacíos y comillas.
ICAL_URLS = [
    u.strip().strip('"').strip("'")
    for u in os.getenv("ICAL_URLS", "").split(",")
    if u.strip().strip('"').strip("'")
]
LAT, LON  = os.getenv("LAT"), os.getenv("LON")

WMO_DESC = {
    0: ("Despejado","☀️"), 1: ("Mayormente despejado","🌤️"), 2: ("Parcialmente nublado","⛅"),
    3: ("Nublado","☁️"), 45: ("Niebla","🌫️"), 48: ("Niebla escarchada","🌫️"),
    51: ("Llovizna ligera","🌦️"), 53: ("Llovizna","🌦️"), 55: ("Llovizna fuerte","🌧️"),
    61: ("Lluvia ligera","🌧️"), 63: ("Lluvia","🌧️"), 65: ("Lluvia fuerte","🌧️"),
    71: ("Nieve ligera","🌨️"), 73: ("Nieve","🌨️"), 75: ("Nieve fuerte","❄️"),
    80: ("Chubascos","🌧️"), 81: ("Chubascos","🌧️"), 82: ("Chubascos fuertes","⛈️"),
    95: ("Tormentas","⛈️"), 96: ("Tormentas con granizo","⛈️"), 99: ("Tormentas con granizo","⛈️"),
}

def geocode(city: str):
    r = requests.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1, "language": "es", "format": "json"},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("results"):
        raise RuntimeError("No se encontraron coordenadas")
    res = data["results"][0]
    return float(res["latitude"]), float(res["longitude"]), res.get("name", city)

def fetch_weather(lat: float, lon: float, tz_name: str):
    """Devuelve el bloque 'daily' (arrays) para hoy y mañana."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        timeout=15,
        params={
            "latitude": lat,
            "longitude": lon,
            "timezone": tz_name,
            "daily": [
                "temperature_2m_max","temperature_2m_min",
                "precipitation_probability_max","sunrise","sunset","weathercode"
            ],
            "forecast_days": 2,
        },
    )
    r.raise_for_status()
    return r.json()["daily"]

def nice_weather_text_2days(city_shown, data):
    """Formatea clima de Hoy y Mañana a partir de arrays 'daily'."""
    lines = [f"🌦️ *Clima — {city_shown}*"]
    for idx, label in enumerate(["Hoy", "Mañana"]):
        try:
            tmin  = data["temperature_2m_min"][idx]
            tmax  = data["temperature_2m_max"][idx]
            pp    = data["precipitation_probability_max"][idx]
            sr    = data["sunrise"][idx][-5:]
            ss    = data["sunset"][idx][-5:]
            wcode = data["weathercode"][idx]
        except (IndexError, KeyError, TypeError):
            continue
        desc, emo = WMO_DESC.get(wcode, ("", ""))
        lines.append(
            f"\n*{label}* — {emo} {desc}\n"
            f"Temp: {tmin}°C – {tmax}°C • Precip.: {pp}%\n"
            f"Amanecer: {sr}  Atardecer: {ss}"
        )
    return "\n".join(lines)

def fetch_ics_events_today(urls, tzname):
    """Lee eventos de HOY desde 0:00 a 24:00 local, tolerante a URLs inválidas."""
    tzlocal = tz.gettz(tzname)
    start = dt.datetime.now(tzlocal).replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + dt.timedelta(days=1)

    def to_local_dt(x):
        # x puede ser datetime o Arrow (ics)
        if hasattr(x, "datetime"):  # Arrow
            d = x.datetime
        else:                       # datetime
            d = x
        if d.tzinfo is None:
            d = d.replace(tzinfo=tz.UTC)  # asumir UTC si es naive
        return d.astimezone(tzlocal)

    events = []
    for raw in urls:
        url = (raw or "").strip().strip('"').strip("'")
        if not url:
            continue
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            cal = Calendar(r.text)  # ics==0.7.2 espera string
        except Exception:
            continue  # ignorar esta URL y seguir

        for e in cal.events:
            if e.begin is None:
                continue
            s = to_local_dt(e.begin)
            t = to_local_dt(e.end or e.begin)
            # incluir si intersecta con hoy
            if t > start and s < end:
                events.append((s, t, e.name, e.all_day, getattr(e, "location", None)))

    events.sort(key=lambda x: x[0])
    return events

def format_agenda(events):
    if not events:
        return "🗓️ *Agenda de hoy*\n(No hay eventos)\n"
    lines = ["🗓️ *Agenda de hoy*"]
    for s, t, name, all_day, loc in events:
        if all_day:
            lines.append(f"• (Todo el día) — *{name}*")
        else:
            h1 = s.strftime("%H:%M")
            mins = int((t - s).total_seconds() // 60)
            hh, mm = divmod(mins, 60)
            dstr = f"{hh}h {mm}m" if hh else f"{mm}m"
            where = f" @ {loc}" if loc else ""
            lines.append(f"{h1} ({dstr}) — *{name}*{where}")
    return "\n".join(lines) + "\n"

async def send_telegram(text: str):
    if not (BOT_TOKEN and CHAT_ID):
        raise RuntimeError("Faltan BOT_TOKEN o CHAT_ID")
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")

def run():
    # Coordenadas: priorizamos LAT/LON; si no hay, geocodificamos CITY
    if LAT and LON:
        lat, lon, city_shown = float(LAT), float(LON), os.getenv("CITY", "")
        if not city_shown:
            city_shown = f"{lat:.4f},{lon:.4f}"
    else:
        lat, lon, city_shown = geocode(CITY)

    daily = fetch_weather(lat, lon, TZ_NAME)
    agenda = fetch_ics_events_today(ICAL_URLS, TZ_NAME)
    msg = nice_weather_text_2days(city_shown, daily) + "\n\n" + format_agenda(agenda)
    asyncio.run(send_telegram(msg))
    # (Opcional) Alerta de paraguas si hoy o mañana >= 50% de precipitación
    try:
        pp_hoy = daily["precipitation_probability_max"][0]
        pp_man = daily["precipitation_probability_max"][1]
        if (pp_hoy is not None and pp_hoy >= 50) or (pp_man is not None and pp_man >= 50):
            msg = "⚠️ *Probabilidad alta de lluvia (≥50%)*. Considerá llevar paraguas.\n\n" + msg
    except Exception:
        pass

    asyncio.run(send_telegram(msg))

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        try:
            asyncio.run(send_telegram(f"⚠️ Error en bot: `{e}`"))
        except:
            pass
        raise
