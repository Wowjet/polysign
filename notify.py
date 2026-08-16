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


def format_signal(sig):
    color = GREEN if sig["type"] in ("FINAL", "ARB") else YELLOW
    head = _c(f"[{sig['type']}]", color)
    p = sig.get("p")
    p_txt = "1.00" if p == 1.0 else f"{p:.2f}" if p is not None else "?"
    edge_txt = _c(f"+{sig['edge'] * 100:.2f}%", GREEN)
    lines = [
        f"{head} {sig['side']} — {sig['title']}",
        (f"   ask {sig['ask']:.3f} × ${sig['usd']:.0f}   "
         f"p≈{p_txt}   edge {edge_txt}   {_c(sig['detail'], DIM)}"),
        f"   {_c('https://polymarket.com/event/' + sig['event_slug'], CYAN)}",
    ]
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
    def __init__(self, log_file="signals.log", telegram=None):
        self.log_file = log_file
        self.telegram = telegram  # {"token":..., "chat_id":...} или None

    def emit(self, sig):
        print(format_signal(sig))
        sys.stdout.flush()
        self._log(sig)
        if self.telegram and self.telegram.get("token") and self.telegram.get("chat_id"):
            plain = (f"[{sig['type']}] {html.escape(str(sig['side']))} — "
                     f"{html.escape(str(sig['title']))}\n"
                     f"ask {sig['ask']:.3f} × ${sig['usd']:.0f}  "
                     f"edge +{sig['edge']*100:.2f}%\n"
                     f"https://polymarket.com/event/{sig['event_slug']}")
            send_telegram(self.telegram["token"], self.telegram["chat_id"], plain)

    def _log(self, sig):
        rec = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            **{k: sig.get(k) for k in ("type", "title", "event_slug", "market_slug",
                                       "side", "token", "ask", "size", "usd",
                                       "p", "edge", "detail")},
        }
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(_c(f"log error: {e}", RED), file=sys.stderr)
