"""Источники данных: Polymarket (Gamma + CLOB) и ESPN (live-счёта). Только stdlib."""
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) polysign/1.0"}
ESPN_HEADERS = {"User-Agent": "Mozilla/5.0"}  # ESPN режет нестандартные UA
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ESPN = "https://site.api.espn.com/apis/site/v2/sports"


def http_json(url, timeout=20, retries=2, headers=None):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- Polymarket

def fetch_league_events(tag, limit=200):
    """Открытые события одной лиги по её тегу. Выборка по тегу лиги полная,
    в отличие от пагинации по общему тегу sports (Gamma её теряет)."""
    events, seen, offset = [], set(), 0
    for _ in range((limit + 99) // 100):
        url = (f"{GAMMA}/events?tag_slug={tag}&closed=false&active=true"
               f"&limit=100&offset={offset}")
        page = http_json(url)
        if not page:
            break
        for ev in page:
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                events.append(ev)
        offset += 100
        if len(page) < 100:
            break
    return events


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_book(token_id):
    book = http_json(f"{CLOB}/book?token_id={token_id}")
    asks = sorted(
        ((float(r["price"]), float(r["size"])) for r in book.get("asks", [])),
        key=lambda x: x[0])
    bids = sorted(
        ((float(r["price"]), float(r["size"])) for r in book.get("bids", [])),
        key=lambda x: -x[0])
    return {"asks": asks, "bids": bids}


# -------------------------------------------------------------------- ESPN

def _norm_game(ev):
    comp = ev["competitions"][0]
    st = comp.get("status", {})
    typ = st.get("type", {})
    home = away = None
    for c in comp.get("competitors", []):
        team = {
            "name": c.get("team", {}).get("displayName", ""),
            "abbr": c.get("team", {}).get("abbreviation", ""),
            "score": int(c.get("score") or 0),
        }
        if c.get("homeAway") == "home":
            home = team
        else:
            away = team
    return {
        "id": str(ev.get("id")),
        "home": home,
        "away": away,
        "state": typ.get("state"),          # pre | in | post
        "detail": typ.get("detail", ""),
        "clock": st.get("displayClock", ""),  # "82'", "3:24", "HT"
        "period": st.get("period", 0) or 0,
        "date": ev.get("date"),
    }


def fetch_espn_games(espn_code, now=None):
    """Игры лиги за вчера и сегодня (по UTC-датам), дедуп по id."""
    now = now or datetime.now(timezone.utc)
    days = [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
    games, seen = [], set()
    for day in days:
        try:
            data = http_json(f"{ESPN}/{espn_code}/scoreboard?dates={day}",
                             timeout=15, retries=1, headers=ESPN_HEADERS)
        except Exception:  # noqa: BLE001
            continue
        for ev in data.get("events", []):
            gid = str(ev.get("id"))
            if gid not in seen:
                seen.add(gid)
                games.append(_norm_game(ev))
        time.sleep(0.15)
    return games
