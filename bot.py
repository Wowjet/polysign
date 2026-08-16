"""polysign: сигнальный бот для Polymarket sports.

Сканирует открытые спортивные события, сверяет со счётом ESPN и подаёт сигналы:
  [FINAL]        игра закончена (ESPN post), а ask на победителя всё ещё < 1
  [LIVE-NEARFINAL] матч практически решён по счёту, ask дешевле оценки p
  [ARB]          сумма асков всех исходов < 1 (негативный риск)
  [BOOK-ONLY]    стакан выглядит как «зашедшая ставка» без подтверждения счётом

Запуск:  python bot.py          (цикл)
         python bot.py --once   (один проход, для проверки)
"""
import argparse
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import analysis
import sources
from notify import Notifier

CONFIG_PATH = "config.json"


def load_config(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _fetch_league_window(tag, window_lo, window_hi):
    """События одной лиги в окне времени (для параллельной загрузки)."""
    out = []
    try:
        league_events = sources.fetch_league_events(tag)
    except Exception as e:  # noqa: BLE001
        print(f"  ! gamma {tag}: {e}", file=sys.stderr)
        return out
    for ev in league_events:
        ts = sources.parse_ts(ev.get("startTime"))
        if ts and window_lo <= ts <= window_hi:
            out.append(ev)
    return out


def _fetch_espn_safe(code, now):
    try:
        return sources.fetch_espn_games(code, now=now)
    except Exception as e:  # noqa: BLE001
        print(f"  ! espn {code}: {e}", file=sys.stderr)
        return None


def scan(cfg, notifier, state):
    t0 = time.time()
    now = datetime.now(timezone.utc)
    window_lo = now - timedelta(hours=cfg["lookback_hours"])
    window_hi = now + timedelta(hours=cfg["forward_hours"])

    # события тянем по тегам лиг (полная выборка), ESPN — только по лигам
    # с матчами в окне; параллельно, иначе проход занимает минуту
    events, espn_needed, espn_cache = [], set(), {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        league_events = dict(zip(
            [row[1] for row in cfg["league_map"]],
            pool.map(lambda row: _fetch_league_window(row[3], window_lo, window_hi),
                     cfg["league_map"])))
    for row in cfg["league_map"]:
        evs = league_events.get(row[1]) or []
        events.extend(evs)
        if evs:
            espn_needed.add(row[1])
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = dict(zip(sorted(espn_needed),
                           pool.map(lambda code: _fetch_espn_safe(code, now),
                                    sorted(espn_needed))))
    espn_cache = {k: v for k, v in fetched.items() if v is not None}

    live_games = sum(1 for games in espn_cache.values() for g in games if g["state"] == "in")
    post_games = sum(1 for games in espn_cache.values() for g in games if g["state"] == "post")
    print(f"{now.strftime('%H:%M:%S')} | событий в окне "
          f"±({cfg['lookback_hours']}ч/{cfg['forward_hours']}ч): {len(events)} | "
          f"ESPN лиг: {len(espn_cache)}, live={live_games}, финалов={post_games} | "
          f"проход {time.time() - t0:.1f}с")

    book_budget = cfg["max_book_requests_per_cycle"]
    signals = []

    for ev in events:
        lg = analysis.league_for_series(ev.get("seriesSlug"), cfg["league_map"])
        teams = analysis.split_event_title(ev.get("title", ""))
        if not teams:
            continue
        game = None
        if lg:
            game = analysis.match_game(teams, espn_cache.get(lg["espn"], []))
        cands = analysis.ml_candidates(ev)
        if not cands:
            continue

        def can_fetch_book():
            nonlocal book_budget
            if book_budget <= 0:
                return False
            book_budget -= 1
            return True

        # --- сигналы по счёту (ESPN) -----------------------------------
        if game and game["state"] in ("in", "post"):
            sport = lg["sport"]
            for cand in cands:
                if book_budget <= 0:
                    break
                p = analysis.estimate_p(game, cand["side"], sport)
                if p is None or p < 0.9:
                    continue
                if not can_fetch_book():
                    break
                try:
                    book = sources.fetch_book(cand["token"])
                except Exception:  # noqa: BLE001
                    continue
                ask = analysis.best_ask_usd(book)
                if not ask:
                    continue
                edge = p - ask["price"]
                if (ask["price"] <= cfg["max_ask"] and edge >= cfg["min_edge"]
                        and ask["usd"] >= cfg["min_liquidity_usd"]):
                    sig = {
                        "type": "FINAL" if game["state"] == "post" and p == 1.0 else "LIVE-NEARFINAL",
                        "title": ev["title"], "event_slug": ev["slug"],
                        "market_slug": cand["market_slug"], "side": cand["side"],
                        "token": cand["token"], "ask": ask["price"], "size": ask["size"],
                        "usd": ask["usd"], "p": p, "edge": edge,
                        "detail": f"{game['home']['score']}:{game['away']['score']} {game['clock']} {sport}",
                    }
                    signals.append(sig)

        # --- негативный риск --------------------------------------------
        if cfg.get("neg_risk_scan") and book_budget > len(cands):
            vol = float(ev.get("volume24hr") or 0)
            if vol >= cfg["neg_risk_min_volume24h"]:
                try:
                    books = [sources.fetch_book(c["token"]) for c in cands]
                except Exception:  # noqa: BLE001
                    books = []
                book_budget -= len(cands)
                asks = [analysis.best_ask_usd(b) for b in books]
                if all(asks) and len(asks) >= 2:
                    total = sum(a["price"] for a in asks)
                    min_usd = min(a["usd"] for a in asks)
                    edge = 1.0 - total
                    if edge >= cfg["min_edge"] and min_usd >= cfg["min_liquidity_usd"]:
                        signals.append({
                            "type": "ARB", "title": ev["title"], "event_slug": ev["slug"],
                            "market_slug": ",".join(c["market_slug"] or "" for c in cands),
                            "side": f"все {len(asks)} исхода", "token": "",
                            "ask": total, "size": 0, "usd": min_usd, "p": 1.0,
                            "edge": edge, "detail": f"сумма асков {total:.3f}",
                        })

        # --- book-only: «зашедшая ставка» без ESPN ----------------------
        if cfg.get("book_only_sweep", {}).get("enabled") and not game:
            bo = cfg["book_only_sweep"]
            for cand in cands:
                mk = next((m for m in ev.get("markets", []) if m.get("slug") == cand["market_slug"]), {})
                bid, ask = mk.get("bestBid"), mk.get("bestAsk")
                if bid is None or ask is None:
                    continue
                if bid >= bo["min_bid"] and ask <= bo["max_ask"] and book_budget > 0:
                    book_budget -= 1
                    try:
                        book = sources.fetch_book(cand["token"])
                    except Exception:  # noqa: BLE001
                        continue
                    a = analysis.best_ask_usd(book)
                    if a and a["usd"] >= cfg["min_liquidity_usd"] and a["price"] <= bo["max_ask"]:
                        signals.append({
                            "type": "BOOK-ONLY", "title": ev["title"], "event_slug": ev["slug"],
                            "market_slug": cand["market_slug"], "side": cand["side"],
                            "token": cand["token"], "ask": a["price"], "size": a["size"],
                            "usd": a["usd"], "p": None,
                            "edge": 1.0 - a["price"], "detail": "без подтверждения счётом",
                        })

    # --- дедуп и выдача ---------------------------------------------
    emitted = 0
    for sig in signals:
        key = sig["token"] or (sig["event_slug"] + sig["type"])
        prev = state.get(key)
        improved = prev and sig["edge"] - prev["edge"] >= cfg["realert_edge_step"]
        cooled = not prev or (now - prev["ts"]).total_seconds() > cfg["cooldown_min"] * 60
        if improved or cooled:
            notifier.emit(sig)
            state[key] = {"ts": now, "edge": sig["edge"]}
            emitted += 1

    if not signals:
        print("  сигналов нет")
    elif emitted == 0:
        print(f"  {len(signals)} сигналов в очереди, все в cooldown")
    return len(events), emitted


def maybe_autocommit(log_file):
    """На сервере (POLYSIGN_AUTO_COMMIT=1) сохраняем signals.log в git,
    чтобы сигналы переживали перезапуски раннера."""
    if os.environ.get("POLYSIGN_AUTO_COMMIT") != "1":
        return

    def git(*args, **kw):
        return subprocess.run(["git", *args], capture_output=True, timeout=60, **kw)

    try:
        git("add", log_file)
        if git("diff", "--cached", "--quiet").returncode == 0:
            return
        git("-c", "user.name=polysign-bot", "-c", "user.email=bot@users.noreply.github.com",
            "commit", "-m", "signals update")
        git("pull", "--rebase", "--autostash")
        git("push")
    except Exception:  # noqa: BLE001
        pass


def acquire_single_instance(port=47891):
    """Лок на локальном порту: второй экземпляр бота не стартует."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return s
    except OSError:
        print("polysign уже запущен (порт занят) — второй экземпляр выходим")
        return None


def main():
    parser = argparse.ArgumentParser(description="polysign — сигналы Polymarket sports")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--config", default=CONFIG_PATH)
    args = parser.parse_args()

    cfg = load_config(args.config)
    lock = acquire_single_instance()
    if lock is None:
        sys.exit(0)
    notifier = Notifier(log_file=cfg["log_file"], telegram=cfg.get("telegram"))
    state = {}

    print(f"polysign | min_edge={cfg['min_edge']*100:.2f}%  "
          f"min_liq=${cfg['min_liquidity_usd']}  interval={cfg['poll_interval_sec']}s")
    while True:
        try:
            scan(cfg, notifier, state)
        except KeyboardInterrupt:
            print("\nstop")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ! ошибка цикла: {e}", file=sys.stderr)
        maybe_autocommit(cfg["log_file"])
        if args.once:
            break
        try:            time.sleep(cfg["poll_interval_sec"])
        except KeyboardInterrupt:
            print("\nstop")
            break


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    main()
