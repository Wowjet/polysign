"""Анализ: разбор событий Polymarket, матчинг с ESPN, оценка вероятности,
расчёт края, негативный риск. Вероятности намеренно консервативные."""
import difflib
import json
import re
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=4096)
def _norm_name(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


@lru_cache(maxsize=8192)
def _sim(a, b):
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb or na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def match_game(poly_teams, games, min_ratio=0.72):
    """ESPN-игра, обе команды которой совпадают с командами из названия события."""
    for g in games:
        if not g["home"] or not g["away"]:
            continue
        espn_teams = [g["home"]["name"], g["away"]["name"]]
        best = []
        for pt in poly_teams:
            scores = [( _sim(pt, et), i) for i, et in enumerate(espn_teams)]
            scores.sort(reverse=True)
            best.append(scores[0])
        if all(s >= min_ratio for s, _ in best) and best[0][1] != best[1][1]:
            return g
    return None


def split_event_title(title):
    for sep in (" vs. ", " vs "):
        if sep in title:
            parts = [p.strip() for p in title.split(sep)]
            if len(parts) == 2:
                return parts
    return []


def _tokens(mk):
    raw = mk.get("clobTokenIds", "[]")
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw


def ml_candidates(ev):
    """Moneyline-рынки события в едином виде.

    Возвращает список dict: side, token, market_slug, question.
    Поддержаны два формата Polymarket:
      * US-спорт: один рынок, question == title, outcomes = [КомандаA, КомандаB]
      * футбол:   три рынка "Will X win on ...?" / "... end in a draw?", Yes = token[0]
    """
    out = []
    title = ev.get("title", "")
    for mk in ev.get("markets", []):
        if mk.get("closed") or not mk.get("active"):
            continue
        outcomes = mk.get("outcomes") or []
        q = mk.get("question", "")
        toks = _tokens(mk)
        if not toks:
            continue
        if q == title and len(outcomes) == 2 and "Yes" not in outcomes:
            # US-формат: токен i соответствует outcomes[i]
            for i, team in enumerate(outcomes):
                if i < len(toks):
                    out.append({"side": team, "token": toks[i],
                                "market_slug": mk.get("slug"), "question": q})
        elif q.startswith("Will ") and (" win on " in q or " end in a draw?" in q):
            if " end in a draw?" in q:
                side = "draw"
            else:
                side = q[len("Will "):].split(" win on ")[0].strip()
            out.append({"side": side, "token": toks[0],
                        "market_slug": mk.get("slug"), "question": q})
    return out


def _clock_sec(text):
    m = re.match(r"^(\d+):(\d+)", text or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _soccer_minute(text):
    m = re.match(r"^(\d+)", text or "")
    return int(m.group(1)) if m else None


def estimate_p(game, side, sport):
    """Оценка вероятности исхода `side` (имя команды или 'draw').

    Возвращает float или None, если оценка невозможна (не матч / рано).
    post -> 1.0/0.0 точно по счёту; in -> консервативные пороги.
    """
    if not game or not game.get("home") or not game.get("away"):
        return None
    hs, as_ = game["home"]["score"], game["away"]["score"]
    hn, an = game["home"]["name"], game["away"]["name"]

    def side_score(name):
        return hs if _sim(name, hn) >= 0.72 else as_

    state = game.get("state")
    if state == "post":
        # ESPN помечает перенесённые/отменённые игры как post — верить нельзя
        detail = (game.get("detail") or "").lower()
        if any(w in detail for w in ("postpon", "cancel", "abandon", "susp", "delay")):
            return None
        if side == "draw":
            return 1.0 if hs == as_ else 0.0
        if hs == as_:
            return 0.0
        winner = hn if hs > as_ else an
        return 1.0 if _sim(side, winner) >= 0.72 else 0.0

    if state != "in":
        return None
    diff = abs(hs - as_)
    if hs > as_:
        leader_name = hn
    elif as_ > hs:
        leader_name = an
    else:
        leader_name = None

    if sport == "soccer":
        minute = _soccer_minute(game.get("clock"))
        if minute is None:
            return None
        stoppage = "+" in (game.get("clock") or "")
        if side == "draw":
            # ничья считается решённой только в самом конце (90+);
            # "45'+X" — это перерыв, до конца ещё полматча
            if hs != as_ or not stoppage:
                return None
            return 0.95 if minute >= 90 else None
        if leader_name is None or _sim(side, leader_name) < 0.72:
            return None
        if (diff >= 2 and minute >= 85) or (diff >= 3 and minute >= 70):
            return 0.99
        if (diff >= 2 and minute >= 80) or (diff >= 3 and minute >= 60) or (diff >= 4 and minute >= 45):
            return 0.98
        return None

    if sport in ("basketball", "hockey", "football"):
        sec = _clock_sec(game.get("clock"))
        if sec is None:
            return None
        period = game.get("period") or 0
        per_len = {"basketball": 720, "hockey": 1200, "football": 900}[sport]
        reg_periods = 4 if sport != "hockey" else 3
        if period > reg_periods:            # овертайм — считаем только текущие секунды
            total = sec
        else:
            total = (reg_periods - period) * per_len + sec
        if leader_name is None or _sim(side, leader_name) < 0.72:
            return None
        if sport == "basketball":
            if diff >= 10 and total <= 120:
                return 0.995
            if diff >= 6 and total <= 45:
                return 0.99
        elif sport == "hockey":
            if diff >= 2 and total <= 60:
                return 0.99
            if diff >= 3 and total <= 300:
                return 0.99
        elif sport == "football":
            if diff >= 17 and total <= 180:
                return 0.995
            if diff >= 9 and total <= 45:
                return 0.99
        return None

    return None  # baseball и прочие in-play не оцениваем — только post


def best_ask_usd(book):
    if not book["asks"]:
        return None
    price, size = book["asks"][0]
    return {"price": price, "size": size, "usd": price * size}


def league_for_series(series_slug, mapping):
    """mapping: [[series_prefix, espn_code, sport, poly_tag], ...]."""
    if not series_slug:
        return None
    for prefix, code, sport, tag in sorted(mapping, key=lambda m: -len(m[0])):
        if series_slug.startswith(prefix):
            return {"espn": code, "sport": sport, "tag": tag}
    return None
