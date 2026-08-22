"""Live-счёты всех матчей из спортивного фида Polymarket.

Поток держит wss://sports-api.polymarket.com/ws — публичный, без авторизации.
На подключение сервер пушит снапшот всех идущих игр, дальше — обновления
по мере хода матчей (гол, смена периода, конец). Это тот же фид, на который
опирается сам Polymarket, поэтому:
  * конец матча виден за секунды (ESPN — через минуты);
  * есть лиги, которых в ESPN нет вовсе (KBO/NPB/CPBL/K-League).

Формат сообщения (поля счёта продублированы плоско и в eventState):
  {"gameId": 90111113, "leagueAbbreviation": "sea",
   "homeTeam": "Udinese Calcio", "awayTeam": "Como 1907",
   "status": "InProgress", "score": "1-0", "elapsed": "51",
   "period": "2H", "live": true, "ended": false}

Поля slug в фиде НЕТ (проверено 2026-08-22) — матчи ищем по именам команд
тем же fuzzy-матчингом, что и для ESPN.
"""
import json
import threading
import time

import analysis

WS_URL = "wss://sports-api.polymarket.com/ws"


def _int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _parse_score(s):
    """'2-1' -> (2, 1); киберспорт '000-000|0-0|Bo3' -> берём первую пару."""
    if not s:
        return None, None
    first = str(s).split("|")[0]
    parts = first.split("-")
    if len(parts) != 2:
        return None, None
    h, a = _int(parts[0]), _int(parts[1])
    return (h, a) if h is not None and a is not None else (None, None)


def to_game(msg):
    """Сообщение фида -> «игра» в формате ESPN-словаря (analysis.estimate_p).

    Так один и тот же оценщик p работает и с ESPN, и с фидом Polymarket.
    Ориентация дом/гости не критична: estimate_p определяет стороны по именам.
    """
    st = msg.get("eventState") or msg
    hs, as_ = _parse_score(st.get("score"))
    state = "post" if st.get("ended") else ("in" if st.get("live") else "pre")
    return {
        "id": str(msg.get("gameId") or ""),
        "home": {"name": msg.get("homeTeam") or "", "abbr": "", "score": hs or 0},
        "away": {"name": msg.get("awayTeam") or "", "abbr": "", "score": as_ or 0},
        "state": state,
        # у фида нет признака переноса/отмены матча — detail пуст, p идёт по счёту
        "detail": "",
        "clock": str(st.get("elapsed") or ""),
        "period": st.get("period") or "",
        # вид спорта из фида ("soccer", ...) — выручает для лиг вне league_map
        "sport": st.get("type") or "",
    }


class SportsStream:
    """Держит карту живых игр фида; сканер спрашивает по именам команд."""

    def __init__(self, on_log=print):
        self._games = {}          # gameId -> «игра» (формат ESPN-словаря)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._open = False
        self._log = on_log

    # ------------------------------------------------------------ API
    def start(self):
        try:
            import websocket  # noqa: F401
        except ImportError:
            self._log("sports-ws: нет websocket-client, работаем без фида")
            return False
        threading.Thread(target=self._run, daemon=True, name="sports-ws").start()
        return True

    def stop(self):
        self._stop.set()

    def healthy(self):
        return self._open

    def find_game(self, teams):
        """Игра фида, обе команды которой совпали с событием Polymarket.

        Допускаем и зеркальное совпадение (поле-команда события оказалась
        в гостях) — оценщику p это безразлично, он матчит по именам.
        """
        with self._lock:
            games = list(self._games.values())
        for g in games:
            names = [g["home"]["name"], g["away"]["name"]]
            if not names[0] or not names[1]:
                continue
            direct = min(analysis._sim(teams[0], names[0]),
                         analysis._sim(teams[1], names[1]))
            crossed = min(analysis._sim(teams[0], names[1]),
                          analysis._sim(teams[1], names[0]))
            if direct >= 0.72 or crossed >= 0.72:
                return g
        return None

    def stats(self):
        with self._lock:
            live = sum(1 for g in self._games.values() if g["state"] == "in")
            done = sum(1 for g in self._games.values() if g["state"] == "post")
            return {"live": live, "done": done, "total": len(self._games)}

    # ------------------------------------------------------- внутреннее
    def _run(self):
        import websocket

        while not self._stop.is_set():
            try:
                self._serve(websocket)
            except Exception as e:  # noqa: BLE001
                self._log(f"sports-ws: обрыв ({e}), реконнект через 5с")
            if self._stop.wait(5):
                break

    def _serve(self, websocket):
        def on_open(ws):
            self._open = True
            self._log("sports-ws: соединение установлено")

        def on_close(ws, code, msg):
            self._open = False
            self._log(f"sports-ws: закрыто ({code})")

        def on_message(ws, m):
            self._handle(m)

        ws = websocket.WebSocketApp(
            WS_URL, on_open=on_open, on_message=on_message,
            on_close=on_close, on_error=lambda w, e: None,
        )
        ws.run_forever(ping_interval=20, ping_timeout=10)
        self._open = False

    def _handle(self, raw):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(data, dict):
            data = [data]
        for msg in data:
            if not isinstance(msg, dict) or "gameId" not in msg:
                continue
            with self._lock:
                self._games[msg["gameId"]] = to_game(msg)
                # защита от роста карты: держим последние 2000 игр
                if len(self._games) > 2000:
                    stale = [gid for gid, g in self._games.items()
                             if g["state"] == "post"]
                    for gid in stale[:len(stale) // 2]:
                        del self._games[gid]
