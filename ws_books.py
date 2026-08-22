"""Живые стаканы CLOB Polymarket через вебсокет (push вместо поллинга).

Поток держит соединение wss://ws-subscriptions-clob.polymarket.com/ws/market,
подписывается на moneyline-токены из окна сканирования и поддерживает
снапшоты стаканов. Сканер читает их без единого REST-запроса; при обрыве
связи снапшоты сбрасываются и бот автоматически уходит на REST-фолбэк.

Особенности протокола (проверено 2026-08-22):
  * текстовый фрейм "PING" каждые ~10с ПОСЛЕ подписки продлевает жизнь
    соединения (сервер отвечает текстовым же "PONG"); но "PING" ДО подписки
    сервер воспринимает как ошибку и рвёт соединение;
  * отписка: {"assets_ids": [...], "type": "unsubscribe"} (плюс дублируем
    "operation" — встречаются оба варианта формата). Если сервер отписку
    проигнорировал, утечку снапшотов режет фильтр _want в _store_snapshot.
"""
import json
import threading
import time

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SYNC_EVERY_SEC = 5
PING_EVERY_SEC = 10


def _levels(rows):
    out = []
    for r in rows or []:
        try:
            p, s = float(r["price"]), float(r["size"])
        except (KeyError, ValueError, TypeError):
            continue
        if s > 0:
            out.append((p, s))
    return out


class BookStream:
    def __init__(self, on_log=print):
        self._books = {}        # token -> {"asks": [...], "bids": [...], "ts": sec}
        self._want = set()
        self._subscribed = set()
        self._last_msg_ts = {}  # token -> ms из события (защита от устаревших)
        self._lock = threading.Lock()
        self._ws = None
        self._open = False
        self._stop = threading.Event()
        self._log = on_log

    # ------------------------------------------------------------ API

    def start(self):
        try:
            import websocket  # noqa: F401
        except ImportError:
            self._log("ws: нет библиотеки websocket-client, работаем по REST")
            return False
        threading.Thread(target=self._run, daemon=True, name="ws-books").start()
        threading.Thread(target=self._sync_loop, daemon=True, name="ws-sync").start()
        return True

    def stop(self):
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:  # noqa: BLE001
            pass

    def set_tokens(self, tokens):
        with self._lock:
            self._want = set(tokens)
            # не копим снапшоты снятых токенов
            for tok in list(self._books):
                if tok not in self._want:
                    del self._books[tok]

    def get(self, token):
        with self._lock:
            b = self._books.get(token)
            return {"asks": b["asks"], "bids": b["bids"]} if b else None

    def healthy(self):
        return self._open

    def stats(self):
        with self._lock:
            return {"snapshots": len(self._books), "wanted": len(self._want)}

    # ------------------------------------------------------- внутреннее

    def _run(self):
        import websocket

        while not self._stop.is_set():
            try:
                self._serve(websocket)
            except Exception as e:  # noqa: BLE001
                self._log(f"ws: обрыв ({e}), реконнект через 5с")
            if self._stop.wait(5):
                break

    def _serve(self, websocket):
        def on_open(ws):
            self._open = True
            with self._lock:
                self._subscribed = set()
            self._log("ws: соединение установлено")
            self._sync_subs(ws)

        def on_close(ws, code, msg):
            self._open = False
            with self._lock:
                self._books.clear()   # данные могли устареть — уходим на REST
            self._log(f"ws: закрыто ({code}), чистим снапшоты")

        def on_error(ws, err):
            pass  # разрыв обработает _run

        def on_message(ws, m):
            self._handle(m)

        self._ws = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_close=on_close,
            on_error=on_error,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)
        self._open = False

    def _sync_loop(self):
        last_ping = 0.0
        while not self._stop.wait(SYNC_EVERY_SEC):
            if self._open and self._ws:
                try:
                    self._sync_subs(self._ws)
                    # keepalive: текстовый PING, но только если уже есть подписки
                    # (PING до первой подписки рвёт соединение — см. докстринг)
                    if self._subscribed and time.time() - last_ping >= PING_EVERY_SEC:
                        self._ws.send("PING")
                        last_ping = time.time()
                except Exception:  # noqa: BLE001
                    pass

    def _sync_subs(self, ws):
        with self._lock:
            to_add = self._want - self._subscribed
            to_del = self._subscribed - self._want
        if to_add:
            # подписываемся пачками: большие списки (900+ токенов) одним
            # фреймом сервер обрабатывает не полностью
            add = sorted(to_add)
            for i in range(0, len(add), 50):
                ws.send(json.dumps({"assets_ids": add[i:i + 50], "type": "market"}))
            with self._lock:
                self._subscribed |= to_add
        if to_del:
            try:
                # дублируем оба известных формата отписки — серверу лишнее не вредит
                ws.send(json.dumps({"assets_ids": list(to_del),
                                    "type": "unsubscribe",
                                    "operation": "unsubscribe"}))
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._subscribed -= to_del

    def _handle(self, raw):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return  # служебные текстовые ответы ("PONG" и т.п.)
        if isinstance(data, dict):
            data = [data]
        for ev in data:
            if not isinstance(ev, dict):
                continue
            et = ev.get("event_type")
            if et == "book" or "bids" in ev or "asks" in ev or "buys" in ev or "sells" in ev:
                self._store_snapshot(ev)
            elif et == "price_change":
                self._apply_delta(ev)

    def _store_snapshot(self, ev):
        token = ev.get("asset_id")
        if not token:
            return
        # фильтр _want: если отписка не сработала, сервер продолжит слать
        # старые токены — не пускаем их в снапшоты (раньше из-за этого
        # счётчик снапшотов обгонял wanted, напр. «ws 321/230»)
        with self._lock:
            if token not in self._want:
                return
        ms = ev.get("timestamp")
        try:
            ms = float(ms)
        except (TypeError, ValueError):
            ms = 0
        if ms and ms < self._last_msg_ts.get(token, 0):
            return  # устаревший снапшот
        if ms:
            self._last_msg_ts[token] = ms
        asks = sorted(_levels(ev.get("asks", ev.get("sells"))), key=lambda x: x[0])
        bids = sorted(_levels(ev.get("bids", ev.get("buys"))), key=lambda x: -x[0])
        with self._lock:
            self._books[token] = {"asks": asks, "bids": bids, "ts": time.time()}

    def _apply_delta(self, ev):
        """Старый/альтернативный формат дельт: применяем к снапшоту."""
        token = ev.get("asset_id")
        changes = list(ev.get("changes") or [])
        for pc in ev.get("price_changes") or []:
            token = token or pc.get("asset_id")
            changes += pc.get("changes") or []
        if not token:
            return
        with self._lock:
            snap = self._books.get(token) or {"asks": [], "bids": [], "ts": time.time()}
            for ch in changes:
                try:
                    price = float(ch["price"])
                    size = float(ch.get("size", 0))
                except (KeyError, ValueError, TypeError):
                    continue
                side = (ch.get("side") or "").upper()
                key = "bids" if side in ("BUY", "BID") else "asks"
                levels = [l for l in snap[key] if l[0] != price]
                if size > 0:
                    levels.append((price, size))
                snap[key] = sorted(levels, key=lambda x: -x[0] if key == "bids" else x[0])
            snap["ts"] = time.time()
            self._books[token] = snap
