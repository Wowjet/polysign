"""polysign: сигнальный бот для Polymarket sports.

Сканирует открытые спортивные события, сверяет со счётом ESPN и подаёт сигналы:
  [FINAL]          игра закончена (ESPN post), а ask на победителя всё ещё < 1
  [LIVE-NEARFINAL] матч практически решён по счёту, ask дешевле оценки p
  [ARB]            сумма асков всех исходов < 1 (негативный риск)
  [BOOK-ONLY]      стакан выглядит как «зашедшая ставка» без подтверждения счётом

Запуск:  python bot.py          (цикл)
         python bot.py --once   (один проход, для проверки)
"""
import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import analysis
import sources
from notify import Notifier


def load_config(path):
    import json
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def maybe_autocommit(log_file):
    """На сервере (POLYSIGN_AUTO_COMMIT=1) сохраняем signals.log в git,
    чтобы сигналы переживали перезапуски раннера."""
    if os.environ.get("POLYSIGN_AUTO_COMMIT") != "1":
        return

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, timeout=60)

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


def rotate_log(path, max_bytes=5_000_000):
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".old")
    except OSError:
        pass


class BookFetcher:
    """Стаканы цикла: бюджет запросов к CLOB + мемо, чтобы токен
    не запрашивался дважды за проход."""

    def __init__(self, budget, workers):
        self.budget = budget
        self.workers = workers
        self.memo = {}
        self.requested = 0

    def get(self, tokens):
        missing = [t for t in tokens if t not in self.memo]
        if missing:
            take = missing[:max(0, self.budget)]
            if take:
                self.memo.update(sources.fetch_books(take, self.workers))
                self.requested += len(take)
                self.budget -= len(take)
            for t in missing[len(take):]:
                self.memo[t] = None
        return [self.memo.get(t) for t in tokens]


def _gamma_market(ev, market_slug):
    for m in ev.get("markets", []):
        if m.get("slug") == market_slug:
            return m
    return {}


def scan(cfg, notifier, state, caches):
    t0 = time.time()
    now = datetime.now(timezone.utc)
    window_lo = now - timedelta(hours=cfg["lookback_hours"])
    window_hi = now + timedelta(hours=cfg["forward_hours"])

    def league_one(row):
        try:
            return row, caches["leagues"].get(row[3])
        except Exception as e:  # noqa: BLE001
            print(f"  ! gamma {row[3]}: {e}", file=sys.stderr)
            return row, None

    events, espn_needed = [], set()
    for row, league_events in sources.parallel_map(league_one, cfg["league_map"], 8):
        if not league_events:
            continue
        in_window = 0
        for ev in league_events:
            ts = sources.parse_ts(ev.get("startTime"))
            if ts and window_lo <= ts <= window_hi:
                events.append(ev)
                in_window += 1
        if in_window:
            espn_needed.add(row[1])

    def espn_one(code):
        try:
            return code, caches["espn"].get(code)
        except Exception as e:  # noqa: BLE001
            print(f"  ! espn {code}: {e}", file=sys.stderr)
            return code, None

    espn_cache = {c: g for c, g in sources.parallel_map(espn_one, sorted(espn_needed), 6)
                  if g is not None}
    empty = [c for c, g in espn_cache.items() if not g]
    if espn_needed and empty:
        print(f"  ? ESPN пусто для {', '.join(empty)} (geo/лимит?)", file=sys.stderr)

    live_games = sum(1 for games in espn_cache.values() for g in games if g["state"] == "in")
    post_games = sum(1 for games in espn_cache.values() for g in games if g["state"] == "post")

    books = BookFetcher(cfg["max_book_requests_per_cycle"], cfg.get("book_workers", 8))
    min_edge, min_usd, max_ask = cfg["min_edge"], cfg["min_liquidity_usd"], cfg["max_ask"]
    cooldown_sec = cfg["cooldown_min"] * 60
    signals = []

    def need_check(key, fp):
        """Нужен ли запрос стакана: нет отпечатка, отпечаток изменился
        или давно не проверяли (раз в кулдаун-окно)."""
        prev = state.get(key)
        if prev is None or prev.get("fp") != fp:
            return True
        return (now - prev.get("fp_ts", prev["ts"])).total_seconds() > cooldown_sec

    def mark(key, fp, edge=None, alert=False):
        prev = state.get(key, {})
        state[key] = {
            "ts": now if alert else prev.get("ts"),
            "fp": fp,
            "fp_ts": now,
            "edge": edge if alert else prev.get("edge"),
        }

    for ev in events:
        lg = analysis.league_for_series(ev.get("seriesSlug"), cfg["league_map"])
        teams = analysis.split_event_title(ev.get("title", ""))
        if not teams:
            continue
        game = analysis.match_game(teams, espn_cache.get(lg["espn"], [])) if lg else None
        cands = analysis.ml_candidates(ev)
        if not cands:
            continue

        # --- сигналы по счёту (ESPN) -----------------------------------
        if game and game["state"] in ("in", "post"):
            scored = []
            for cand in cands:
                p = analysis.estimate_p(game, cand["side"], lg["sport"])
                if p is None or p < 0.9:
                    continue
                ga = _gamma_market(ev, cand["market_slug"]).get("bestAsk")
                # префильтр: стакан бывает лишь чуть лучше gamma-аска
                if ga is not None and p - ga < min_edge - 0.02:
                    continue
                fp = f"{ga}|{p}|{game['state']}"
                if need_check(cand["token"], fp):
                    scored.append((cand, p, fp))
            if scored:
                got = books.get([c[0]["token"] for c in scored])
                for (cand, p, fp), book in zip(scored, got):
                    ask = analysis.best_ask_usd(book) if book else None
                    if ask and ask["price"] <= max_ask and p - ask["price"] >= min_edge \
                            and ask["usd"] >= min_usd:
                        signals.append({
                            "type": "FINAL" if game["state"] == "post" and p == 1.0 else "LIVE-NEARFINAL",
                            "title": ev["title"], "event_slug": ev["slug"],
                            "market_slug": cand["market_slug"], "side": cand["side"],
                            "token": cand["token"], "ask": ask["price"], "size": ask["size"],
                            "usd": ask["usd"], "p": p, "edge": p - ask["price"],
                            "detail": f"{game['home']['score']}:{game['away']['score']} "
                                      f"{game['clock']} {lg['sport']}",
                            "_fp": fp,
                        })
                    else:
                        mark(cand["token"], fp)

        # --- негативный риск --------------------------------------------
        if cfg.get("neg_risk_scan"):
            vol = float(ev.get("volume24hr") or 0)
            if vol >= cfg["neg_risk_min_volume24h"]:
                gamma_asks = [_gamma_market(ev, c["market_slug"]).get("bestAsk") for c in cands]
                if all(a is not None for a in gamma_asks):
                    fp = round(sum(gamma_asks), 4)
                    key = ev["slug"] + "ARB"
                    if fp < 1 - min_edge + 0.02 and need_check(key, fp):
                        got = books.get([c["token"] for c in cands])
                        asks = [analysis.best_ask_usd(b) if b else None for b in got]
                        if all(asks) and len(asks) >= 2:
                            total = sum(a["price"] for a in asks)
                            min_size = min(a["usd"] for a in asks)
                            edge = 1.0 - total
                            if edge >= min_edge and min_size >= min_usd:
                                signals.append({
                                    "type": "ARB", "title": ev["title"], "event_slug": ev["slug"],
                                    "market_slug": ",".join(c["market_slug"] or "" for c in cands),
                                    "side": f"все {len(asks)} исхода", "token": "",
                                    "ask": total, "size": 0, "usd": min_size, "p": 1.0,
                                    "edge": edge, "detail": f"сумма асков {total:.3f}",
                                    "_fp": fp,
                                })
                            else:
                                mark(key, fp)

        # --- book-only: «зашедшая ставка» без ESPN ----------------------
        if cfg.get("book_only_sweep", {}).get("enabled") and not game:
            bo = cfg["book_only_sweep"]
            for cand in cands:
                mk = _gamma_market(ev, cand["market_slug"])
                bid, ask_g = mk.get("bestBid"), mk.get("bestAsk")
                if bid is None or ask_g is None or bid < bo["min_bid"] or ask_g > bo["max_ask"]:
                    continue
                if not need_check(cand["token"], ask_g):
                    continue
                book = books.get([cand["token"]])[0]
                a = analysis.best_ask_usd(book) if book else None
                if a and a["usd"] >= min_usd and a["price"] <= bo["max_ask"]:
                    signals.append({
                        "type": "BOOK-ONLY", "title": ev["title"], "event_slug": ev["slug"],
                        "market_slug": cand["market_slug"], "side": cand["side"],
                        "token": cand["token"], "ask": a["price"], "size": a["size"],
                        "usd": a["usd"], "p": None,
                        "edge": 1.0 - a["price"], "detail": "без подтверждения счётом",
                        "_fp": ask_g,
                    })
                else:
                    mark(cand["token"], ask_g)

    # --- дедуп и выдача ---------------------------------------------
    emitted = 0
    for sig in signals:
        key = sig["token"] or (sig["event_slug"] + sig["type"])
        prev = state.get(key)
        prev_edge = prev.get("edge") if prev else None
        improved = prev_edge is not None and sig["edge"] - prev_edge >= cfg["realert_edge_step"]
        cooled = not prev or not prev.get("ts") or \
            (now - prev["ts"]).total_seconds() > cooldown_sec
        if improved or cooled:
            notifier.emit(sig)
            mark(key, sig.get("_fp"), edge=sig["edge"], alert=True)
            emitted += 1
        else:
            # сигнал валиден, но в кулдауне — обновляем отпечаток, чтобы
            # стакан не перечитывался каждый цикл
            mark(key, sig.get("_fp"))

    if len(state) > 4000:
        cutoff = now - timedelta(hours=12)
        for k in [k for k, v in state.items()
                  if (v.get("fp_ts") or v.get("ts") or now) < cutoff]:
            del state[k]

    print(f"{now.strftime('%H:%M:%S')} | событий {len(events)} | ESPN {len(espn_cache)} лиг, "
          f"live={live_games}, финалов={post_games} | стаканов {books.requested} | "
          f"проход {time.time() - t0:.1f}с"
          + ("" if signals else " | сигналов нет"))
    if signals and emitted == 0:
        print(f"  {len(signals)} сигналов в очереди, все в cooldown")
    return len(events), emitted


def main():
    parser = argparse.ArgumentParser(description="polysign — сигналы Polymarket sports")
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    cfg = load_config(args.config)
    lock = None
    if not args.once:  # разовый проход можно делать параллельно с основным
        lock = acquire_single_instance()
        if lock is None:
            sys.exit(0)
    rotate_log(cfg["log_file"])
    notifier = Notifier(log_file=cfg["log_file"], telegram=cfg.get("telegram"))
    caches = {
        "leagues": sources.LeagueCache(cfg.get("cache_league_sec", 120)),
        "espn": sources.EspnCache(cfg.get("cache_espn_sec", 40)),
    }
    state = {}

    print(f"polysign | min_edge={cfg['min_edge']*100:.2f}%  "
          f"min_liq=${cfg['min_liquidity_usd']}  interval={cfg['poll_interval_sec']}s")
    while True:
        try:
            scan(cfg, notifier, state, caches)
        except KeyboardInterrupt:
            print("\nstop")
            break
        except Exception as e:  # noqa: BLE001
            print(f"  ! ошибка цикла: {e}", file=sys.stderr)
        maybe_autocommit(cfg["log_file"])
        if args.once:
            break
        try:
            time.sleep(cfg["poll_interval_sec"])
        except KeyboardInterrupt:
            print("\nstop")
            break


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    main()
