"""polysign: сигнальный бот для Polymarket sports.

Сканирует открытые спортивные события, сверяет со счётом ESPN и подаёт сигналы:
  [FINAL]          игра закончена (ESPN post), а ask на победителя всё ещё < 1
  [LIVE-NEARFINAL] матч практически решён по счёту, ask дешевле оценки p
  [ARB]            сумма асков всех исходов < 1 (негативный риск)
  [BOOK-ONLY]      стакан выглядит как «зашедшая ставка» без подтверждения счётом

Стаканы читаются из живого вебсокета CLOB (push при каждом изменении);
если вебсокет недоступен — автоматический REST-фолбэк.

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
from sports_ws import SportsStream
from ws_books import BookStream


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
    """REST-фолбэк для стаканов: бюджет запросов к CLOB + мемо за проход."""

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


def depth_text(book, max_price, max_levels=3):
    """Живые котировки для сигнала: верхние уровни аска до max_price + лучший бид.

    Зачем: «ask 0.981 × $984» — это только первый уровень; на самом деле
    купить можно больше, но дороже (0.985×$180, 0.99×$520, ...). Бид рядом
    с 1.0 подтверждает, что рынок и правда считает исход решённым
    (в [BOOK-ONLY] это фактически единственное «подтверждение»).
    """
    if not book:
        return None
    # уровни аска в пределах цены, где ещё есть край (до max_price)
    levels = [(p, p * s) for p, s in book["asks"] if p <= max_price]
    total = sum(usd for _, usd in levels)
    parts = ", ".join(f"{p:.3f}×${usd:.0f}" for p, usd in levels[:max_levels])
    bid = book["bids"][0] if book["bids"] else None
    bid_txt = f"bid {bid[0]:.3f}×${bid[0] * bid[1]:.0f}" if bid else "бидов нет"
    more = f" (+${total - levels[0][1]:.0f} дальше)" if len(levels) > 1 else ""
    return f"аск: {parts or '—'}{more} | {bid_txt}"


def take_sig_fields(book, p, min_edge):
    """Готовые поля «плана захода» для одиночного сигнала (FINAL/LIVE/BOOK-ONLY).

    Главная строка: ЗАБРАТЬ $X → профит ~$Y (+Z%) — сколько долларов можно
    купить по всему стакану, пока край держится не ниже min_edge, и сколько
    на этом заработаешь. levels — поуровневая раскладка для проверки глазами.
    """
    plan = analysis.take_plan(book, p, min_edge)
    if not plan or plan["usd"] < 1:
        return None
    pct = plan["profit"] / plan["usd"] * 100 if plan["usd"] else 0
    levels_txt = " · ".join(f"{p_:.3f}×${usd:.0f} (+${sh * p - usd:.0f})"
                            for p_, usd, sh in plan["levels"])
    return {
        "take_text": (f"ЗАБРАТЬ ${plan['usd']:.0f} ({plan['shares']:.0f} ш. по "
                      f"{plan['avg']:.3f}) → профит ~${plan['profit']:.0f} (+{pct:.1f}%)"),
        "take_levels": levels_txt,
        "take_usd": round(plan["usd"], 2),
        "take_profit": round(plan["profit"], 2),
    }


def market_link(event_slug, market_slug):
    """Ссылка на конкретный рынок события (для футбольных «Will X win»).
    US-формат: рынок один и слаг обычно совпадает с событием — хватит /event/."""
    if market_slug and market_slug != event_slug:
        return f"https://polymarket.com/event/{event_slug}/{market_slug}"
    return f"https://polymarket.com/event/{event_slug}"


def _group_pending(name, game):
    """Спутниковая группа («1-й тайм», «2-й тайм») ещё НЕ решена?

    Гвард от ложных арбитражей: после свистка в тайме котировки решённого
    рынка могут висеть устаревшими, и сумма асков «случайно» даст < 1.
    Поэтому тайм-группу сканируем только до её determining-момента.
    """
    if game is None or game.get("state") == "pre":
        return True
    if game.get("state") == "post":
        return False
    per = str(game.get("period") or "")
    minute = analysis._soccer_minute(game.get("clock"))
    if name == "1-й тайм":
        return per in ("1H", "1", "PERIOD 1") or (minute is not None and minute < 45)
    return True  # «2-й тайм» решается только с финальным свистком


def scan(cfg, notifier, state, caches, stream=None, sports=None):
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
        if in_window and row[1]:  # лиги без ESPN (null) идут только по стакану
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

    # --- контекст по событиям + желаемые токены для вебсокета ------------
    entries, desired = [], set()
    for ev in events:
        lg = analysis.league_for_series(ev.get("seriesSlug"), cfg["league_map"])
        teams = analysis.split_event_title(ev.get("title", ""))
        if not teams:
            continue
        # Три источника счёта, по убыванию приоритета:
        #   1) спортивный фид Polymarket уже видит КОНЕЦ матча -> мгновенный
        #      FINAL (ESPN узнаёт о финале на минуты позже);
        #   2) ESPN: богаче всего — знает переносы/отмены (post-гвард в
        #      estimate_p) и периоды, им доверяем для живых матчей;
        #   3) живой счёт фида Polymarket для лиг, которых в ESPN нет
        #      (KBO/NPB/...): хотя бы LIVE-оценка по минуте/счёту.
        # Оба формата «игры» совместимы с analysis.estimate_p.
        sports_game = sports.find_game(teams) if sports else None
        espn_game = analysis.match_game(teams, espn_cache.get(lg["espn"], [])) \
            if lg and lg.get("espn") else None
        if sports_game and sports_game["state"] == "post":
            game = sports_game
        elif espn_game:
            game = espn_game
        else:
            game = sports_game
        cands = analysis.ml_candidates(ev)
        groups = analysis.outcome_groups(ev)
        if not cands and not groups:
            continue
        entries.append((ev, lg, game, cands, groups))
        desired.update(c["token"] for c in cands)
        # токены групп для негативного риска — тоже в вебсокет. Спутниковые
        # события («... - Halftime Result») несут свои группы: таймы и пр.
        for grp in groups:
            if grp["name"] == "победитель" or _group_pending(grp["name"], game):
                desired.update(c["token"] for c in grp["cands"])
    if stream:
        stream.set_tokens(list(desired)[: cfg.get("ws_max_tokens", 600)])

    books = BookFetcher(cfg["max_book_requests_per_cycle"], cfg.get("book_workers", 8))
    min_edge, min_usd, max_ask = cfg["min_edge"], cfg["min_liquidity_usd"], cfg["max_ask"]
    cooldown_sec = cfg["cooldown_min"] * 60
    signals = []

    def live_book(token):
        return stream.get(token) if stream else None

    def need_check(key, fp):
        """Нужен ли REST-запрос стакана: нет отпечатка, изменился
        или давно не проверяли (раз в кулдаун-окно)."""
        prev = state.get(key)
        if prev is None or prev.get("fp") != fp:
            return True
        return (now - prev.get("fp_ts", prev["ts"])).total_seconds() > cooldown_sec

    def mark(key, fp, edge=None, usd=None, alert=False):
        prev = state.get(key, {})
        state[key] = {
            "ts": now if alert else prev.get("ts"),
            "fp": fp,
            "fp_ts": now,
            "edge": edge if alert else prev.get("edge"),
            "usd": usd if alert else prev.get("usd"),
        }

    def rest_book(token, fp):
        """REST-запрос с префильтром отпечатков (когда ws не отдал снапшот)."""
        if fp is not None and not need_check(token, fp):
            return None
        book = books.get([token])[0]
        if book is not None and fp is not None:
            mark(token, fp)
        return book

    for ev, lg, game, cands, groups in entries:
        # --- сигналы по счёту (ESPN или спортивный фид Polymarket) --------
        if game and game["state"] in ("in", "post"):
            # вид спорта: из league_map, а для неразмеченных лиг — из фида
            sport = lg["sport"] if lg else game.get("sport") or "soccer"
            for cand in cands:
                p = analysis.estimate_p(game, cand["side"], sport)
                if p is None or p < 0.9:
                    continue
                ga = _gamma_market(ev, cand["market_slug"]).get("bestAsk")
                fp = f"{ga}|{p}|{game['state']}"
                book = live_book(cand["token"])
                rest_used = False
                if book is None:
                    # REST-префильтр: стакан бывает лишь чуть лучше gamma-аска
                    if ga is not None and p - ga < min_edge - 0.02:
                        continue
                    book = rest_book(cand["token"], fp)
                    rest_used = True
                ask = analysis.best_ask_usd(book) if book else None
                if ask and ask["price"] <= max_ask and p - ask["price"] >= min_edge \
                        and ask["usd"] >= min_usd:
                    signals.append({
                        "type": "FINAL" if game["state"] == "post" and p == 1.0 else "LIVE-NEARFINAL",
                        "title": ev["title"], "event_slug": ev["slug"],
                        "market_slug": cand["market_slug"], "side": cand["side"],
                        "token": cand["token"], "ask": ask["price"], "size": ask["size"],
                        "usd": ask["usd"], "p": p, "edge": p - ask["price"],
                        # ожидаемая прибыль: размер в шарах × эдж (usd/ask = число шаров)
                        "profit": ask["usd"] / ask["price"] * (p - ask["price"]),
                        "detail": f"{game['home']['score']}:{game['away']['score']} "
                                  f"{game['clock']} {sport}",
                        "depth": depth_text(book, max_ask),
                        # p=1.0 (FINAL) — профит гарантирован; p<1 — матожидание
                        **(take_sig_fields(book, p, min_edge) or {}),
                        "url": market_link(ev["slug"], cand["market_slug"]),
                        "_fp": fp if rest_used else None,
                    })

        # --- негативный риск: по ВСЕМ полным группам исходов события -------
        # Кроме «победителя» арбитражируются «1-й тайм» и «2-й тайм»: в группе
        # ровно один исход случается, значит сумма асков < 1 = гарантированный
        # профит. У ARB свои пороги (arb_min_edge/arb_min_usd): гарантия
        # позволяет брать края поменьше, чем для рискованных сигналов.
        if cfg.get("neg_risk_scan"):
            arb_edge = cfg.get("arb_min_edge", min_edge)
            arb_usd = cfg.get("arb_min_usd", min_usd)
            vol = float(ev.get("volume24hr") or 0)
            for grp in groups:
                gc = grp["cands"]
                if grp["name"] != "победитель" and not _group_pending(grp["name"], game):
                    continue
                snapshots = [live_book(c["token"]) for c in gc]
                fp = None
                if stream and stream.healthy() and all(snapshots):
                    # живые снапшоты — проверяем всё без объёмного фильтра
                    asks = [analysis.best_ask_usd(s) for s in snapshots]
                elif grp["name"] == "победитель" and vol >= cfg["neg_risk_min_volume24h"]:
                    # REST-путь только для «победителя» (гамма-префильтр суммы)
                    gamma_asks = [_gamma_market(ev, c["market_slug"]).get("bestAsk") for c in gc]
                    if not all(a is not None for a in gamma_asks):
                        continue
                    fp = round(sum(gamma_asks), 4)
                    key = ev["slug"] + "ARB"
                    if fp >= 1 - arb_edge + 0.02 or not need_check(key, fp):
                        continue
                    got = books.get([c["token"] for c in gc])
                    asks = [analysis.best_ask_usd(b) if b else None for b in got]
                    if not all(asks):
                        mark(key, fp)
                else:
                    continue
                if not (asks and all(asks) and len(asks) >= 2):
                    continue
                total = sum(a["price"] for a in asks)
                min_size = min(a["usd"] for a in asks)
                edge = 1.0 - total
                if edge < arb_edge or min_size < arb_usd:
                    continue
                # Глубокий план: берём уровни аска каждого исхода, пока средняя
                # цена набора остаётся ≤ 1 − arb_edge (больше $ на событие,
                # чем только лучшие аски). Требует живых снапшотов.
                plan = None
                if stream and stream.healthy() and all(snapshots):
                    plan = analysis.arb_take_plan(snapshots, arb_edge)
                n_best = int(min(a["size"] for a in asks))
                if plan and plan["n"] > n_best:
                    n, spend, profit = plan["n"], plan["spend"], plan["profit"]
                else:
                    n, spend = n_best, sum(n_best * a["price"] for a in asks)
                    profit = n - spend
                take_text = (f"ЗАБРАТЬ {n} наборов (набор = по 1 ш. каждого исхода): "
                             f"вложить ${spend:.0f} → гарантированно ${n} → "
                             f"профит +${profit:.2f} (+{profit / spend * 100:.1f}%)")
                orders = " | ".join(
                    f"{c['side']}: {a['price']:.3f}×{n} ш. ≈ ${n * a['price']:.0f}"
                    for c, a in zip(gc, asks))
                links = [f"{c['side']} → {market_link(ev['slug'], c['market_slug'])}"
                         for c in gc]
                side = f"все {len(asks)} исхода" if grp["name"] == "победитель" \
                    else f"{grp['name']}: все {len(asks)} исхода"
                signals.append({
                    "type": "ARB", "group": grp["name"],
                    "title": ev["title"], "event_slug": ev["slug"],
                    "market_slug": ",".join(c["market_slug"] or "" for c in gc),
                    "side": side, "token": "",
                    "ask": total, "size": 0, "usd": min_size, "p": 1.0,
                    "edge": edge, "profit": profit,
                    "detail": f"сумма асков {total:.3f}",
                    "take_text": take_text, "take_levels": orders, "links": links,
                    "take_usd": round(spend, 2), "take_profit": round(profit, 2),
                    "_fp": fp,
                })

        # --- book-only: «зашедшая ставка» без счёта ----------------------
        # Сюда попадаем, только если игра НЕ сматчилась ни со счётом ESPN,
        # ни со спортивным фидом Polymarket. Причина важна:
        #   * лига не покрыта ESPN и фид молчит — счёта не будет никогда;
        #   * оба фида моргнули/отстают — счёт, скорее всего, вот-вот придёт.
        # Второй случай безопаснее: матч скорее всего идёт и решается.
        if lg is None:
            no_score_reason = "лига не размечена в league_map"
        elif lg.get("espn"):
            no_score_reason = "нет ни в ESPN, ни в фиде Polymarket (фиды моргают)"
        else:
            no_score_reason = f"лига {lg['tag']} не покрывается ESPN, фид молчит"
        if cfg.get("book_only_sweep", {}).get("enabled") and not game:
            bo = cfg["book_only_sweep"]
            for cand in cands:
                mk = _gamma_market(ev, cand["market_slug"])
                bid, ask_g = mk.get("bestBid"), mk.get("bestAsk")
                if bid is None or ask_g is None or bid < bo["min_bid"] or ask_g > bo["max_ask"]:
                    continue
                book = live_book(cand["token"])
                rest_used = False
                if book is None:
                    if not need_check(cand["token"], ask_g):
                        continue
                    book = books.get([cand["token"]])[0]
                    rest_used = True
                a = analysis.best_ask_usd(book) if book else None
                if a and a["usd"] >= min_usd and a["price"] <= bo["max_ask"]:
                    edge_val = 1.0 - a["price"]
                    signals.append({
                        "type": "BOOK-ONLY", "title": ev["title"], "event_slug": ev["slug"],
                        "market_slug": cand["market_slug"], "side": cand["side"],
                        "token": cand["token"], "ask": a["price"], "size": a["size"],
                        "usd": a["usd"], "p": None,
                        "edge": edge_val, "profit": a["usd"] * edge_val,
                        "detail": f"без подтверждения счётом · {no_score_reason}",
                        "depth": depth_text(book, bo["max_ask"]),
                        # BOOK-ONLY: считаем от p=1.0 — во что верит сам стакан
                        # (бид ≥ min_bid); реальный исход может отличаться
                        **(take_sig_fields(book, 1.0, min_edge) or {}),
                        "url": market_link(ev["slug"], cand["market_slug"]),
                        "_fp": ask_g if rest_used else None,
                    })
                elif rest_used:
                    mark(cand["token"], ask_g)

    # --- дедуп и выдача ---------------------------------------------
    emitted = 0
    for sig in signals:
        # у ARB токена нет — ключ включает группу («победитель»/«1-й тайм»/...),
        # чтобы разные группы одного события дедупились независимо
        key = sig["token"] or (sig["event_slug"] + sig["type"] + str(sig.get("group") or ""))
        prev = state.get(key)
        prev_edge = prev.get("edge") if prev else None
        prev_usd = prev.get("usd") if prev else None
        improved = prev_edge is not None and sig["edge"] - prev_edge >= cfg["realert_edge_step"]
        # ликвидность ощутимо подросла — стоит сказать заново
        grown = prev_usd is not None and sig["usd"] >= max(prev_usd * 1.5, prev_usd + 20)
        cooled = not prev or not prev.get("ts") or \
            (now - prev["ts"]).total_seconds() > cooldown_sec
        if improved or grown or cooled:
            notifier.emit(sig)
            mark(key, sig.get("_fp"), edge=sig["edge"], usd=sig["usd"], alert=True)
            emitted += 1
        else:
            mark(key, sig.get("_fp"))

    if len(state) > 4000:
        cutoff = now - timedelta(hours=12)
        for k in [k for k, v in state.items()
                  if (v.get("fp_ts") or v.get("ts") or now) < cutoff]:
            del state[k]

    ws = stream.stats() if stream else {"snapshots": 0, "wanted": 0}
    ws_txt = f" | ws {ws['snapshots']}/{ws['wanted']}" if stream else ""
    sp = sports.stats() if sports else None
    sp_txt = f" | фид {sp['live']} живых/{sp['done']} финалов" if sp else ""
    print(f"{now.strftime('%H:%M:%S')} | событий {len(entries)} | ESPN {len(espn_cache)} лиг, "
          f"live={live_games}, финалов={post_games}{sp_txt}{ws_txt} | "
          f"REST-стаканов {books.requested} | "
          f"проход {time.time() - t0:.1f}с" + ("" if signals else " | сигналов нет"))
    if signals and emitted == 0:
        print(f"  {len(signals)} сигналов в очереди, все в cooldown")
    return len(entries), emitted


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
    # Telegram: на сервере креды приходят из GitHub Secrets, не из конфига
    tg = cfg.get("telegram")
    if os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT_ID"):
        tg = {"token": os.environ["TG_TOKEN"], "chat_id": os.environ["TG_CHAT_ID"]}
    notifier = Notifier(
        log_file=cfg["log_file"], telegram=tg,
        sound=cfg.get("sound", False) and not args.once,
    )
    caches = {
        "leagues": sources.LeagueCache(cfg.get("cache_league_sec", 120)),
        "espn": sources.EspnCache(cfg.get("cache_espn_sec", 40)),
    }

    stream = None
    if cfg.get("ws_enabled", True):
        stream = BookStream(on_log=lambda s: print(s, flush=True))
        if not stream.start():
            stream = None

    # спортивный фид Polymarket: live-счёты всех матчей (см. sports_ws.py)
    sports = None
    if cfg.get("sports_ws_enabled", True):
        sports = SportsStream(on_log=lambda s: print(s, flush=True))
        if not sports.start():
            sports = None

    state = {}
    print(f"polysign | min_edge={cfg['min_edge']*100:.2f}%  "
          f"min_liq=${cfg['min_liquidity_usd']}  interval={cfg['poll_interval_sec']}s"
          + ("  +websocket" if stream else "  (REST)")
          + ("  +sports-ws" if sports else ""))
    try:
        while True:
            try:
                scan(cfg, notifier, state, caches, stream, sports)
            except Exception as e:  # noqa: BLE001
                print(f"  ! ошибка цикла: {e}", file=sys.stderr)
            maybe_autocommit(cfg["log_file"])
            if args.once:
                break
            time.sleep(cfg["poll_interval_sec"])
    except KeyboardInterrupt:
        print("\nstop")
    finally:
        if sports:
            sports.stop()
        if stream:
            stream.stop()
        if lock:
            lock.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    main()
