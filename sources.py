"""Источники данных: Polymarket (Gamma + CLOB) и ESPN (live-счёта). Только stdlib."""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) polysign/1.0",
    "Accept": "application/json",
}
ESPN_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
# site.web.api (хост фронтенда espn.com) вместо site.api: датацентровые IP
# (GitHub Actions) Akamai режет 403 на site.api, а web-хост отдаёт то же самое
ESPN = "https://site.web.api.espn.com/apis/site/v2/sports"


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


def parallel_map(fn, items, max_workers=8):
    """Параллельный вызов fn по items; fn сам обязан ловить свои ошибки."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(fn, items))


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


class LeagueCache:
    """Список событий лиги меняется редко — перечитываем раз в ttl."""

    def __init__(self, ttl_sec):
        self.ttl = ttl_sec
        self._data = {}

    def get(self, tag):
        now = time.monotonic()
        hit = self._data.get(tag)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        data = fetch_league_events(tag)
        self._data[tag] = (now, data)
        return data


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


def fetch_books(tokens, max_workers=8):
    """Стаканы пачкой параллельно; ошибки -> None для токена."""
    def one(tok):
        try:
            return tok, fetch_book(tok)
        except Exception:  # noqa: BLE001
            return tok, None

    return dict(parallel_map(one, list(tokens), max_workers))


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
    """Сетка лиги: текущий день ESPN + вчера и сегодня по UTC-датам.

    Запрос без dates на части CDN отдаёт полную текущую сетку, которую
    dates-фильтр иногда теряет, поэтому объединяем все три источника.
    """
    now = now or datetime.now(timezone.utc)
    days = ["", (now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
    games, seen = [], set()
    for day in days:
        q = f"?dates={day}" if day else ""
        try:
            data = http_json(f"{ESPN}/{espn_code}/scoreboard{q}",
                             timeout=15, retries=1, headers=ESPN_HEADERS)
        except Exception:  # noqa: BLE001
            continue
        for ev in data.get("events", []):
            gid = str(ev.get("id"))
            if gid not in seen:
                seen.add(gid)
                games.append(_norm_game(ev))
    return games


class EspnCache:
    """Счёт обновляется десятками секунд — кэшируем чуть меньше интервала опроса."""

    def __init__(self, ttl_sec):
        self.ttl = ttl_sec
        self._data = {}

    def get(self, espn_code):
        now = time.monotonic()
        hit = self._data.get(espn_code)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        data = fetch_espn_games(espn_code)
        self._data[espn_code] = (now, data)
        return data
