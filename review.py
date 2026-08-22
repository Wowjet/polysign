"""Проверка сигналов из signals.log: зашли или нет.

python review.py            — сводка по всем сигналам
python review.py --last 50  — только последние 50 строк лога
"""
import argparse
import json
import sys

import sources


def load_signals(path, last_n=None):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"лог {path} не найден")
        return []
    if last_n:
        lines = lines[-last_n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def fetch_market(event_slug, market_slug):
    """Итоговое состояние рынка. Читаем через /events: эндпоинт /markets
    перестаёт отдавать закрытые рынки, а событие хранит их цены навсегда."""
    try:
        evs = sources.http_json(
            f"https://gamma-api.polymarket.com/events?slug={event_slug}",
            timeout=15, retries=1)
        for mk in (evs[0] if evs else {}).get("markets", []):
            if mk.get("slug") == market_slug:
                return mk
    except Exception:  # noqa: BLE001
        pass
    return None


def side_price(mk, side):
    """Итоговая цена исхода `side` в рынке (1.0 = исход случился, 0.0 = нет).

    У Polymarket два формата moneyline-рынков, отсюда два пути поиска:
      * US-спорт: один рынок с outcomes = [КомандаA, КомандаB] —
        ищем цену прямо по имени команды;
      * футбол: три рынка "Will X win on ...?" с outcomes = [Yes, No] —
        бот всегда сигналит на Yes-токен (analysis.ml_candidates берёт
        toks[0]), поэтому если команды в outcomes нет, берём цену "Yes".
    """
    prices = mk.get("outcomePrices")
    outs = mk.get("outcomes")
    if isinstance(prices, str):
        prices = json.loads(prices)
    if isinstance(outs, str):
        outs = json.loads(outs)
    if not prices or not outs:
        return None
    lowered = [str(o).lower() for o in outs]
    idx = next((i for i, o in enumerate(lowered) if o == str(side).lower()), None)
    if idx is None and "yes" in lowered:
        # сторона в сигнале — название команды, а рынок футбольный (Yes/No):
        # наш сигнал всегда означает "Yes" (команда выиграет / будет ничья)
        idx = lowered.index("yes")
    return float(prices[idx]) if idx is not None and idx < len(prices) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="signals.log")
    ap.add_argument("--last", type=int, default=None)
    args = ap.parse_args()

    signals = [s for s in load_signals(args.log, args.last)
               if s.get("type") in ("FINAL", "LIVE-NEARFINAL", "BOOK-ONLY") and s.get("token")]
    if not signals:
        print("проверяемых сигналов в логе нет")
        return

    # уникальный сигнал = token + ask (повторные алерты того же уровня не считаем)
    uniq = {(s["token"], round(s.get("ask", 0), 3)): s for s in signals}
    mk_cache = {}

    print(f"{'дата':17}{'тип':15}{'ask':>6}  {'резул.':>7}  сторона")
    n_win = n_lose = n_pending = 0
    pnl = 0.0  # на $1, вложенный в каждый уникальный сигнал
    for _, s in sorted(uniq.items(), key=lambda kv: kv[1]["ts"]):
        slug = (s.get("market_slug") or "").split(",")[0]
        if slug not in mk_cache:
            mk_cache[slug] = fetch_market(s.get("event_slug"), slug)
        mk = mk_cache[slug]
        sp = side_price(mk, s.get("side")) if mk else None
        if sp is None:
            res = "?"
        elif sp >= 0.99:
            res = "WIN"
        elif sp <= 0.01:
            res = "LOSE"
        else:
            res = "..."
        if res == "WIN":
            n_win += 1
            pnl += (1 - s["ask"]) / s["ask"]
        elif res == "LOSE":
            n_lose += 1
            pnl -= 1.0
        else:
            n_pending += 1
        print(f"{s['ts'][:16]:17}{s['type']:15}{s['ask']:6.3f}  {res:>7}  "
              f"{str(s.get('side', ''))[:28]} · {str(s.get('title', ''))[:38]}")

    print(f"\nитого: зашло {n_win}, не зашло {n_lose}, ожидает/неясно {n_pending}")
    if n_win + n_lose:
        print(f"винрейт: {n_win / (n_win + n_lose) * 100:.1f}%")
    print(f"гипотетический PnL: {pnl:+.3f} на $1 в каждый уникальный сигнал")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    main()
