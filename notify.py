"""Вывод сигналов: консоль (цвет), опционально Telegram, лог signals.log (JSONL)."""
import html
import json
import sys
import urllib.request
from datetime import datetime

USE_COLOR = sys.stdout.isatty()

GREEN, YELLOW, CYAN, RED, DIM, RESET = ("\033[92m", "\033[93m", "\033[96m",
                                        "\033[91m", "\033[2m", "\033[0m")


def _c(text, color):
    return f"{color}{text}{RESET}" if USE_COLOR else str(text)


def _beep():
    """Звуковой сигнал: winsound на Windows, консольный BEL иначе."""
    try:
        import winsound
        for _ in range(2):
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            import time as _t
            _t.sleep(0.25)
        return
    except Exception:  # noqa: BLE001
        pass
    print("\a", end="", file=sys.stderr)


def format_signal(sig):
    color = GREEN if sig["type"] in ("FINAL", "ARB") else YELLOW
    head = _c(f"[{sig['type']}]", color)
    p = sig.get("p")
    p_txt = "1.00" if p == 1.0 else f"{p:.2f}" if p is not None else "?"
    edge_txt = _c(f"+{sig['edge'] * 100:.2f}%", GREEN)
    profit = sig.get("profit")
    profit_txt = _c(f"~${profit:.0f}", GREEN) if profit is not None else ""
    depth = sig.get("depth")
    lines = [
        f"{head} {sig['side']} — {sig['title']}",
        (f"   ask {sig['ask']:.3f} × ${sig['usd']:.0f}   "
         f"p≈{p_txt}   edge {edge_txt}   профит {profit_txt}   "
         f"{_c(sig['detail'], DIM)}"),
        f"   {_c('https://polymarket.com/event/' + sig['event_slug'], CYAN)}",
    ]
    # котировки стакана (верхние уровни аска + лучший бид), если бот их приложил
    if depth:
        lines.insert(2, f"   {_c(depth, DIM)}")
    return "\n".join(lines)


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:  # noqa: BLE001
        print(_c(f"telegram error: {e}", RED), file=sys.stderr)
        return False


class Notifier:
    def __init__(self, log_file="signals.log", telegram=None, sound=False):
        self.log_file = log_file
        self.telegram = telegram  # {"token":..., "chat_id":...} или None
        self.sound = sound

    def emit(self, sig):
        print(format_signal(sig))
        sys.stdout.flush()
        if self.sound:
            _beep()
        self._log(sig)
        if self.telegram and self.telegram.get("token") and self.telegram.get("chat_id"):
            profit = sig.get("profit")
            profit_txt = f" | профит ~${profit:.0f}" if profit is not None else ""
            depth_txt = f"\n{html.escape(str(sig['depth']))}" if sig.get("depth") else ""
            plain = (f"🎯 <b>[{html.escape(str(sig['type']))}]</b> "
                     f"{html.escape(str(sig['side']))}\n"
                     f"{html.escape(str(sig['title']))}\n"
                     f"ask {sig['ask']:.3f} × ${sig['usd']:.0f} | "
                     f"edge +{sig['edge']*100:.2f}%{profit_txt}\n"
                     f"{html.escape(str(sig['detail']))}{depth_txt}\n"
                     f"{datetime.now().strftime('%H:%M:%S')}\n"
                     f"https://polymarket.com/event/{sig['event_slug']}")
            send_telegram(self.telegram["token"], self.telegram["chat_id"], plain)

    def _log(self, sig):
        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            **{k: sig.get(k) for k in ("type", "title", "event_slug", "market_slug",
                                       "side", "token", "ask", "size", "usd",
                                       "p", "edge", "profit", "detail", "depth")},
        }
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(_c(f"log error: {e}", RED), file=sys.stderr)
