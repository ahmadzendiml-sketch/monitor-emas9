import asyncio
import os
import zlib
from datetime import datetime, timedelta
from typing import Optional, List, Set, Any
from contextlib import asynccontextmanager
from collections import deque
from functools import lru_cache

try:
    import orjson
    def json_dumps(obj) -> str:
        return orjson.dumps(obj).decode('utf-8')
    def json_dumps_bytes(obj) -> bytes:
        return orjson.dumps(obj)
    def json_loads(data) -> Any:
        return orjson.loads(data)
except ImportError:
    import json
    def json_dumps(obj) -> str:
        return json.dumps(obj, separators=(',', ':'))
    def json_dumps_bytes(obj) -> bytes:
        return json.dumps(obj, separators=(',', ':')).encode('utf-8')
    def json_loads(data) -> Any:
        return json.loads(data)

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import HTMLResponse
from fastapi.middleware.gzip import GZipMiddleware

try:
    import aiohttp
    USE_AIOHTTP = True
except ImportError:
    import httpx
    USE_AIOHTTP = False

try:
    from lxml import html as lxml_html
    USE_LXML = True
except ImportError:
    from bs4 import BeautifulSoup
    USE_LXML = False

MAX_HISTORY = 1441
MAX_USD_HISTORY = 11
API_POLL_INTERVAL = 0.02
USD_POLL_INTERVAL = 0.3
BROADCAST_DEBOUNCE = 0.008
MAX_CONNECTIONS = 500
BROADCAST_CHUNK_SIZE = 50
STATE_CACHE_TTL = 0.05

history: deque = deque(maxlen=MAX_HISTORY)
usd_idr_history: deque = deque(maxlen=MAX_USD_HISTORY)
last_buy: Optional[int] = None
shown_updates: Set[str] = set()
treasury_info: str = "Belum ada info treasury."

HARI_INDO = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")

telegram_app = None
aiohttp_session: Optional["aiohttp.ClientSession"] = None


class StateCache:
    __slots__ = ('_cache', '_cache_time', '_lock', '_version')
    
    def __init__(self):
        self._cache: Optional[bytes] = None
        self._cache_time: float = 0
        self._lock = asyncio.Lock()
        self._version: int = 0
    
    def invalidate(self):
        self._version += 1
        self._cache = None
    
    async def get_state_bytes(self) -> bytes:
        now = asyncio.get_event_loop().time()
        if self._cache and (now - self._cache_time) < STATE_CACHE_TTL:
            return self._cache
        async with self._lock:
            if self._cache and (now - self._cache_time) < STATE_CACHE_TTL:
                return self._cache
            self._cache = build_full_state_bytes()
            self._cache_time = now
            return self._cache
    
    def get_state_bytes_sync(self) -> bytes:
        if self._cache:
            return self._cache
        self._cache = build_full_state_bytes()
        self._cache_time = asyncio.get_event_loop().time()
        return self._cache


state_cache = StateCache()


class ConnectionManager:
    __slots__ = ('_connections', '_write_lock')
    
    def __init__(self):
        self._connections: Set[WebSocket] = set()
        self._write_lock = asyncio.Lock()
    
    async def connect(self, ws: WebSocket) -> bool:
        if len(self._connections) >= MAX_CONNECTIONS:
            return False
        self._connections.add(ws)
        return True
    
    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)
    
    @property
    def count(self) -> int:
        return len(self._connections)
    
    async def broadcast(self, message: bytes):
        if not self._connections:
            return
        connections = list(self._connections)
        failed = []
        for i in range(0, len(connections), BROADCAST_CHUNK_SIZE):
            chunk = connections[i:i + BROADCAST_CHUNK_SIZE]
            results = await asyncio.gather(
                *[self._send_safe(ws, message) for ws in chunk],
                return_exceptions=True
            )
            for ws, result in zip(chunk, results):
                if result is False or isinstance(result, Exception):
                    failed.append(ws)
        for ws in failed:
            self.disconnect(ws)
    
    async def _send_safe(self, ws: WebSocket, message: bytes) -> bool:
        try:
            await asyncio.wait_for(ws.send_bytes(message), timeout=5.0)
            return True
        except:
            return False


manager = ConnectionManager()


@lru_cache(maxsize=1024)
def format_rupiah(n: int) -> str:
    return f"{n:,}".replace(",", ".")


@lru_cache(maxsize=512)
def get_day_time(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return f"{HARI_INDO[dt.weekday()]} {dt.strftime('%H:%M:%S')}"
    except:
        return date_str


def format_waktu_only(date_str: str, status: str) -> str:
    return f"{get_day_time(date_str)}{status}"


@lru_cache(maxsize=256)
def format_diff_display(diff: int, status: str) -> str:
    if status == "🚀":
        return f"🚀+{format_rupiah(diff)}"
    elif status == "🔻":
        return f"🔻-{format_rupiah(abs(diff))}"
    return "➖tetap"


def format_transaction_display(buy: str, sell: str, diff_display: str) -> str:
    return f"Beli: {buy}<br>Jual: {sell}<br>{diff_display}"


PROFIT_CONFIGS = [
    (20000000, 19314000),
    (30000000, 28980000),
    (40000000, 38652000),
    (50000000, 48325000),
]


def calc_profit(h: dict, modal: int, pokok: int) -> str:
    try:
        buy_rate = h["buying_rate"]
        sell_rate = h["selling_rate"]
        gram = modal / buy_rate
        val = int(gram * sell_rate - pokok)
        gram_str = f"{gram:,.4f}".replace(",", ".")
        if val > 0:
            return f"+{format_rupiah(val)}🟢➺{gram_str}gr"
        elif val < 0:
            return f"-{format_rupiah(abs(val))}🔴➺{gram_str}gr"
        return f"{format_rupiah(0)}➖➺{gram_str}gr"
    except:
        return "-"


def build_single_history_item(h: dict) -> dict:
    buy_fmt = format_rupiah(h["buying_rate"])
    sell_fmt = format_rupiah(h["selling_rate"])
    diff_display = format_diff_display(h.get("diff", 0), h["status"])
    return {
        "buying_rate": buy_fmt,
        "selling_rate": sell_fmt,
        "waktu_display": format_waktu_only(h["created_at"], h["status"]),
        "diff_display": diff_display,
        "transaction_display": format_transaction_display(buy_fmt, sell_fmt, diff_display),
        "created_at": h["created_at"],
        "jt20": calc_profit(h, *PROFIT_CONFIGS[0]),
        "jt30": calc_profit(h, *PROFIT_CONFIGS[1]),
        "jt40": calc_profit(h, *PROFIT_CONFIGS[2]),
        "jt50": calc_profit(h, *PROFIT_CONFIGS[3]),
    }


def build_history_data() -> List[dict]:
    return [build_single_history_item(h) for h in history]


def build_usd_idr_data() -> List[dict]:
    return [{"price": h["price"], "time": h["time"]} for h in usd_idr_history]


def build_full_state_bytes() -> bytes:
    return json_dumps_bytes({
        "history": build_history_data(),
        "usd_idr_history": build_usd_idr_data(),
        "treasury_info": treasury_info
    })


async def get_aiohttp_session() -> "aiohttp.ClientSession":
    global aiohttp_session
    if aiohttp_session is None or aiohttp_session.closed:
        timeout = aiohttp.ClientTimeout(total=6, connect=2, sock_read=4)
        connector = aiohttp.TCPConnector(
            limit=150,
            limit_per_host=50,
            keepalive_timeout=120,
            enable_cleanup_closed=True,
            force_close=False,
            ttl_dns_cache=300,
        )
        aiohttp_session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            raise_for_status=False
        )
    return aiohttp_session


async def close_aiohttp_session():
    global aiohttp_session
    if aiohttp_session and not aiohttp_session.closed:
        await aiohttp_session.close()
        await asyncio.sleep(0.1)
        aiohttp_session = None


_treasury_headers = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://treasury.id",
    "Referer": "https://treasury.id/"
}


async def fetch_treasury_price() -> Optional[dict]:
    try:
        session = await get_aiohttp_session()
        async with session.post(
            "https://api.treasury.id/api/v1/antigrvty/gold/rate",
            headers=_treasury_headers
        ) as resp:
            if resp.status == 200:
                return await resp.json(loads=json_loads)
    except:
        pass
    return None


_google_headers = {"Accept": "text/html,application/xhtml+xml"}
_google_cookies = {"CONSENT": "YES+cb.20231208-04-p0.en+FX+410"}


async def fetch_usd_idr_price() -> Optional[str]:
    try:
        session = await get_aiohttp_session()
        async with session.get(
            "https://www.google.com/finance/quote/USD-IDR",
            headers=_google_headers,
            cookies=_google_cookies
        ) as resp:
            if resp.status == 200:
                text = await resp.text()
                if USE_LXML:
                    tree = lxml_html.fromstring(text)
                    divs = tree.xpath('//div[contains(@class, "YMlKec") and contains(@class, "fxKbKc")]')
                    if divs:
                        return divs[0].text_content().strip()
                else:
                    soup = BeautifulSoup(text, "lxml")
                    div = soup.find("div", class_="YMlKec fxKbKc")
                    if div:
                        return div.text.strip()
    except:
        pass
    return None


class BroadcastDebouncer:
    __slots__ = ('_pending', '_lock', '_last_broadcast')
    
    def __init__(self):
        self._pending = False
        self._lock = asyncio.Lock()
        self._last_broadcast: float = 0
    
    async def schedule_broadcast(self):
        async with self._lock:
            if self._pending:
                return
            self._pending = True
        state_cache.invalidate()
        await asyncio.sleep(BROADCAST_DEBOUNCE)
        async with self._lock:
            self._pending = False
        message = await state_cache.get_state_bytes()
        await manager.broadcast(message)
        self._last_broadcast = asyncio.get_event_loop().time()


debouncer = BroadcastDebouncer()


async def api_loop():
    global last_buy, shown_updates
    consecutive_errors = 0
    while True:
        try:
            result = await fetch_treasury_price()
            if result:
                consecutive_errors = 0
                data = result.get("data", {})
                buy = data.get("buying_rate")
                sell = data.get("selling_rate")
                upd = data.get("updated_at")
                if buy and sell and upd and upd not in shown_updates:
                    buy, sell = int(float(buy)), int(float(sell))
                    diff = 0 if last_buy is None else buy - last_buy
                    if last_buy is None:
                        status = "➖"
                    elif buy > last_buy:
                        status = "🚀"
                    elif buy < last_buy:
                        status = "🔻"
                    else:
                        status = "➖"
                    history.append({
                        "buying_rate": buy,
                        "selling_rate": sell,
                        "status": status,
                        "diff": diff,
                        "created_at": upd
                    })
                    last_buy = buy
                    shown_updates.add(upd)
                    if len(shown_updates) > 5000:
                        shown_updates = {upd}
                    asyncio.create_task(debouncer.schedule_broadcast())
            else:
                consecutive_errors += 1
            await asyncio.sleep(API_POLL_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception:
            consecutive_errors += 1
            await asyncio.sleep(min(0.1 * consecutive_errors, 2.0))


async def usd_idr_loop():
    while True:
        try:
            price = await fetch_usd_idr_price()
            if price:
                should_update = (
                    not usd_idr_history or 
                    usd_idr_history[-1]["price"] != price
                )
                if should_update:
                    wib = datetime.utcnow() + timedelta(hours=7)
                    usd_idr_history.append({
                        "price": price, 
                        "time": wib.strftime("%H:%M:%S")
                    })
                    asyncio.create_task(debouncer.schedule_broadcast())
            await asyncio.sleep(USD_POLL_INTERVAL)
        except asyncio.CancelledError:
            break
        except:
            await asyncio.sleep(1.0)


async def heartbeat_loop():
    ping_msg = b'{"ping":true}'
    while True:
        try:
            await asyncio.sleep(15.0)
            if manager.count > 0:
                await manager.broadcast(ping_msg)
        except asyncio.CancelledError:
            break
        except:
            pass


async def start_telegram_bot():
    global telegram_app, treasury_info
    try:
        from telegram.ext import ApplicationBuilder, CommandHandler
        from telegram import Update
        from telegram.ext import ContextTypes
    except ImportError:
        return None
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        return None
    async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Bot aktif! Gunakan /atur <teks>")
    async def atur_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global treasury_info
        text = update.message.text.partition(' ')[2]
        if text:
            treasury_info = text.replace("  ", "&nbsp;&nbsp;").replace("\n", "<br>")
            asyncio.create_task(debouncer.schedule_broadcast())
            await update.message.reply_text("Info Treasury diubah!")
        else:
            await update.message.reply_text("Gunakan: /atur <kalimat>")
    try:
        telegram_app = ApplicationBuilder().token(token).build()
        telegram_app.add_handler(CommandHandler("start", start_handler))
        telegram_app.add_handler(CommandHandler("atur", atur_handler))
        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(
            drop_pending_updates=True, 
            allowed_updates=["message"]
        )
        return telegram_app
    except:
        return None


async def stop_telegram_bot():
    global telegram_app
    if telegram_app:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except:
            pass
        telegram_app = None


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5">
<title>Harga Emas Treasury</title>
<link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css"/>
<style>
*{box-sizing:border-box}
body{font-family:Arial,sans-serif;margin:0;padding:5px 20px 0 20px;background:#fff;color:#222;transition:background .3s,color .3s}
h2{margin:0 0 2px}
h3{margin:20px 0 10px}
.header{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:2px}
#jam{font-size:1.3em;color:#ff1744;font-weight:bold;margin-bottom:8px}
table.dataTable{width:100%!important}
table.dataTable thead th{font-weight:bold;white-space:nowrap;padding:10px 8px}
table.dataTable tbody td{padding:8px;white-space:nowrap}
th.waktu,td.waktu{width:100px;min-width:90px;max-width:1050px;text-align:left}
th.profit,td.profit{width:154px;min-width:80px;max-width:160px;text-align:left}
.theme-toggle-btn{padding:0;border:none;border-radius:50%;background:#222;color:#fff;cursor:pointer;font-size:1.5em;width:44px;height:44px;display:flex;align-items:center;justify-content:center;transition:background .3s}
.theme-toggle-btn:hover{background:#444}
.dark-mode{background:#181a1b!important;color:#e0e0e0!important}
.dark-mode #jam{color:#ffb300!important}
.dark-mode table.dataTable,.dark-mode table.dataTable thead th,.dark-mode table.dataTable tbody td{background:#23272b!important;color:#e0e0e0!important}
.dark-mode table.dataTable thead th{color:#ffb300!important}
.dark-mode .theme-toggle-btn{background:#ffb300;color:#222}
.dark-mode .theme-toggle-btn:hover{background:#ffd54f}
.container-flex{display:flex;gap:15px;flex-wrap:wrap;margin-top:10px}
.card{border:1px solid #ccc;border-radius:6px;padding:10px}
.card-usd{width:248px;height:370px;overflow-y:auto}
.card-info{width:218px;height:378px;overflow-y:auto}
.card-chart{overflow:hidden;height:370px;width:620px}
.card-calendar{overflow:hidden;height:470px;width:650px}
#priceList{list-style:none;padding:0;margin:0;max-height:275px;overflow-y:auto}
#priceList li{margin-bottom:1px}
.time{color:gray;font-size:.9em;margin-left:10px}
#currentPrice{color:red;font-weight:bold}
.dark-mode #currentPrice{color:#00E124;text-shadow:1px 1px #00B31C}
#tabel tbody tr:first-child td{color:red!important;font-weight:bold}
.dark-mode #tabel tbody tr:first-child td{color:#00E124!important}
#isiTreasury{white-space:pre-line;color:red;font-weight:bold;max-height:376px;overflow-y:auto;scrollbar-width:none;-ms-overflow-style:none;word-break:break-word}
#isiTreasury::-webkit-scrollbar{display:none}
.dark-mode #isiTreasury{color:#00E124}
.chart-iframe{border:0;width:100%;display:block}
#footerApp{width:100%;position:fixed;bottom:0;left:0;background:transparent;text-align:center;z-index:100;padding:8px 0}
.marquee-text{display:inline-block;color:#F5274D;animation:marquee 70s linear infinite;font-weight:bold}
.dark-mode .marquee-text{color:#B232B2}
@keyframes marquee{0%{transform:translateX(100vw)}100%{transform:translateX(-100%)}}
.loading-text{color:#999;font-style:italic}
.tbl-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch}
.dataTables_wrapper{position:relative}
.dt-top-controls{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:0!important;padding:8px 0;padding-bottom:0!important}
.dataTables_wrapper .dataTables_length{margin:0!important;float:none!important;margin-bottom:0!important;padding-bottom:0!important}
.dataTables_wrapper .dataTables_filter{margin:0!important;float:none!important}
.dataTables_wrapper .dataTables_info{display:none!important}
.dataTables_wrapper .dataTables_paginate{margin-top:10px!important;text-align:center!important}
.tbl-wrap{margin-top:0!important;padding-top:0!important}
#tabel.dataTable{margin-top:0!important}
.tradingview-section{margin-top:0px;clear:both}
.tradingview-wrapper{height:400px;overflow:hidden;border:1px solid #ccc;border-radius:6px}
.tradingview-wrapper iframe{width:100%;height:100%;border:0}
#tabel tbody td.transaksi{line-height:1.3;padding:6px 8px}
#tabel tbody td.transaksi .harga-beli{display:block;margin-bottom:2px}
#tabel tbody td.transaksi .harga-jual{display:block;margin-bottom:2px}
#tabel tbody td.transaksi .selisih{display:block;font-weight:bold}
@media(max-width:768px){
body{padding:12px;padding-bottom:50px}
h2{font-size:1.1em}
h3{font-size:1em;margin:15px 0 8px}
.header{margin-bottom:2px}
#jam{font-size:1.5em;margin-bottom:6px}
table.dataTable{font-size:13px;min-width:620px}
table.dataTable thead th{padding:8px 6px}
table.dataTable tbody td{padding:6px}
.theme-toggle-btn{width:40px;height:40px;font-size:1.3em}
.container-flex{flex-direction:column;gap:15px}
.card-usd,.card-info,.card-chart,.card-calendar{width:100%!important;max-width:100%!important;min-width:0!important}
.card-usd{height:auto;min-height:320px}
.card-info{height:auto;min-height:300px}
.card-chart{height:380px}
.card-chart iframe{height:440px!important;margin-top:-60px}
.card-calendar{height:450px}
.card-calendar iframe{height:100%!important}
.tradingview-section{margin-top:15px}
.tradingview-section h3{margin:10px 0 8px}
.tradingview-wrapper{height:350px}
.dt-top-controls{flex-direction:row;justify-content:space-between;gap:5px;margin-bottom:8px;padding:5px 0}
.dataTables_wrapper .dataTables_length{font-size:12px!important}
.dataTables_wrapper .dataTables_filter{font-size:12px!important}
.dataTables_wrapper .dataTables_filter input{width:100px!important;font-size:12px!important;padding:4px 6px!important}
.dataTables_wrapper .dataTables_length select{font-size:12px!important;padding:3px!important}
.dataTables_wrapper .dataTables_paginate .paginate_button{padding:4px 10px!important;font-size:12px!important;min-width:auto!important}
#tabel{min-width:580px!important}
#tabel tbody td{font-size:12px!important;padding:5px 4px!important}
#tabel tbody td.waktu{width:85px!important;min-width:85px!important;max-width:85px!important}
#tabel tbody td.transaksi{width:140px!important;min-width:140px!important;max-width:140px!important}
#tabel tbody td.profit{width:120px!important;min-width:120px!important;max-width:120px!important}
#tabel tbody td.transaksi .harga-beli,#tabel tbody td.transaksi .harga-jual,#tabel tbody td.transaksi .selisih{font-size:11px!important;margin-bottom:1px!important}
}
@media(max-width:480px){
body{padding:10px;padding-bottom:45px}
h2{font-size:1em}
h3{font-size:0.95em;margin:12px 0 8px}
.header{margin-bottom:1px}
#jam{font-size:1.3em;margin-bottom:5px}
table.dataTable{font-size:12px;min-width:560px}
table.dataTable thead th{padding:6px 4px}
table.dataTable tbody td{padding:5px 4px}
th.waktu, td.waktu { width: 60px; min-width: 50px; max-width: 70px; }
.theme-toggle-btn{width:36px;height:36px;font-size:1.2em}
.container-flex{gap:12px}
.card{padding:8px}
.card-usd{min-height:280px}
.card-info{min-height:260px}
.card-chart{height:340px}
.card-chart iframe{height:400px!important;margin-top:-58px}
.card-calendar{height:400px}
.tradingview-section{margin-top:12px}
.tradingview-section h3{margin:8px 0 6px}
.tradingview-wrapper{height:300px}
#footerApp{padding:5px 0}
.marquee-text{font-size:12px}
.dt-top-controls{gap:3px;margin-bottom:6px}
.dataTables_wrapper .dataTables_length,.dataTables_wrapper .dataTables_filter{font-size:11px!important}
.dataTables_wrapper .dataTables_filter input{width:80px!important;font-size:11px!important}
.dataTables_wrapper .dataTables_length select{font-size:11px!important}
.dataTables_wrapper .dataTables_paginate .paginate_button{padding:3px 8px!important;font-size:11px!important}
#priceList{max-height:200px}
#tabel{min-width:540px!important}
#tabel tbody td{font-size:11px!important;padding:4px 3px!important}
#tabel tbody td.waktu{width:80px!important;min-width:80px!important;max-width:80px!important}
#tabel tbody td.transaksi{width:130px!important;min-width:130px!important;max-width:130px!important}
#tabel tbody td.profit{width:110px!important;min-width:110px!important;max-width:110px!important}
#tabel tbody td.transaksi .harga-beli,#tabel tbody td.transaksi .harga-jual,#tabel tbody td.transaksi .selisih{font-size:10px!important;margin-bottom:0!important}
}
</style>
</head>
<body>
<div class="header">
<h2>MONITORING Harga Emas Treasury</h2>
<button class="theme-toggle-btn" id="themeBtn" onclick="toggleTheme()" title="Ganti Tema">🌙</button>
</div>
<div id="jam"></div>
<div class="tbl-wrap">
<table id="tabel" class="display">
<thead>
  <tr>
    <th class="waktu">Waktu</th>
    <th>Data Transaksi</th>
    <th class="profit">Est. cuan 20 JT ➺ gr</th>
    <th class="profit">Est. cuan 30 JT ➺ gr</th>
    <th class="profit">Est. cuan 40 JT ➺ gr</th>
    <th class="profit">Est. cuan 50 JT ➺ gr</th>
  </tr>
</thead>
<tbody></tbody>
</table>
</div>
<div class="tradingview-section">
<h3>Chart Harga Emas (XAU/USD)</h3>
<div class="tradingview-wrapper" id="tradingview_chart"></div>
</div>
<div class="container-flex">
<div>
<h3>Harga USD/IDR Google Finance</h3>
<div class="card card-usd" style="margin-top:0;padding-top:2px">
<p>Harga saat ini: <span id="currentPrice" class="loading-text">Memuat data...</span></p>
<h4>Harga Terakhir:</h4>
<ul id="priceList"><li class="loading-text">Menunggu data...</li></ul>
</div>
</div>
<div>
<h3 style="display:block;margin-top:30px">Chart Harga USD/IDR Investing - Jangka Waktu 15 Menit</h3>
<div class="card card-chart">
<iframe class="chart-iframe" src="https://sslcharts.investing.com/index.php?force_lang=54&pair_ID=2138&timescale=900&candles=80&style=candles" height="430" style="margin-top:-62px" loading="lazy"></iframe>
</div>
</div>
</div>
<div class="container-flex">
<div>
<h3>Sekilas Ingfo Treasury</h3>
<div class="card card-info" style="margin-top:0;padding-top:2px">
<ul id="isiTreasury" style="list-style:none;padding-left:0"></ul>
</div>
</div>
<div>
<h3 style="display:block;margin-top:30px">Kalender Ekonomi</h3>
<div class="card card-calendar">
<iframe class="chart-iframe" src="https://sslecal2.investing.com?columns=exc_flags,exc_currency,exc_importance,exc_actual,exc_forecast,exc_previous&category=_employment,_economicActivity,_inflation,_centralBanks,_confidenceIndex&importance=3&features=datepicker,timezone,timeselector,filters&countries=5,37,48,35,17,36,26,12,72&calType=week&timeZone=27&lang=54" height="467" loading="lazy"></iframe>
</div>
</div>
</div>
<footer id="footerApp"><span class="marquee-text">&copy;2026 ~ahmadkholil~</span></footer>
<script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
<script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>
(function(){
var isDark=localStorage.getItem('theme')==='dark';
var lastDataHash='';
var messageQueue=[];
var isProcessing=false;

function createTradingViewWidget(){
var wrapper=document.getElementById('tradingview_chart');
var h=wrapper.offsetHeight||400;
new TradingView.widget({width:"100%",height:h,symbol:"OANDA:XAUUSD",interval:"15",timezone:"Asia/Jakarta",theme:isDark?'dark':'light',style:"1",locale:"id",toolbar_bg:"#f1f3f6",enable_publishing:false,hide_top_toolbar:false,save_image:false,container_id:"tradingview_chart"})
}

var table=$('#tabel').DataTable({
pageLength:4,
lengthMenu:[4,8,18,48,88,888,1441],
order:[],
deferRender:true,
dom:'<"dt-top-controls"lf>t<"bottom"p><"clear">',
columns:[{data:"waktu"},{data:"transaction"},{data:"jt20"},{data:"jt30"},{data:"jt40"},{data:"jt50"}],
language:{emptyTable:"Menunggu data harga emas dari Treasury...",zeroRecords:"Tidak ada data yang cocok",lengthMenu:"Show _MENU_",search:"Search:"}
});

function hashData(h){
if(!h||!h.length)return'';
var f=h[0];
return f.created_at+'|'+f.buying_rate+'|'+h.length;
}

function updateTable(h){
if(!h||!h.length)return;
var newHash=hashData(h);
if(newHash===lastDataHash)return;
lastDataHash=newHash;
h.sort(function(a,b){return new Date(b.created_at)-new Date(a.created_at)});
var arr=h.map(function(d){
return{
waktu:d.waktu_display,
transaction:'<div class="transaksi"><span class="harga-beli">Harga Beli: '+d.buying_rate+'</span><span class="harga-jual"> Jual: '+d.selling_rate+'</span><span class="selisih">'+d.diff_display+'</span></div>',
jt20:d.jt20,jt30:d.jt30,jt40:d.jt40,jt50:d.jt50
}
});
table.clear().rows.add(arr).draw(false);
table.page('first').draw(false);
}

function updateUsd(h){
var c=document.getElementById("currentPrice"),p=document.getElementById("priceList");
if(!h||!h.length){c.textContent="Menunggu data...";c.className="loading-text";p.innerHTML='<li class="loading-text">Menunggu data...</li>';return}
c.className="";
function prs(s){return parseFloat(s.trim().replace(/\./g,'').replace(',','.'))}
var r=h.slice().reverse();
var icon="➖";
if(r.length>1){var n=prs(r[0].price),pr=prs(r[1].price);icon=n>pr?"🚀":n<pr?"🔻":"➖"}
c.innerHTML=r[0].price+" "+icon;
var html='';
for(var i=0;i<r.length;i++){
var ic="➖";
if(i===0&&r.length>1){var n=prs(r[0].price),pr=prs(r[1].price);ic=n>pr?"🟢":n<pr?"🔴":"➖"}
else if(i<r.length-1){var n=prs(r[i].price),nx=prs(r[i+1].price);ic=n>nx?"🟢":n<nx?"🔴":"➖"}
else if(r.length>1){var n=prs(r[i].price),pr=prs(r[i-1].price);ic=n<pr?"🔴":n>pr?"🟢":"➖"}
html+='<li>'+r[i].price+' <span class="time">('+r[i].time+')</span> '+ic+'</li>';
}
p.innerHTML=html;
}

function updateInfo(i){document.getElementById("isiTreasury").innerHTML=i||"Belum ada info treasury."}

function processMessage(d){
if(d.ping)return;
if(d.history)updateTable(d.history);
if(d.usd_idr_history)updateUsd(d.usd_idr_history);
if(d.treasury_info!==undefined)updateInfo(d.treasury_info);
}

function processQueue(){
if(isProcessing||!messageQueue.length)return;
isProcessing=true;
var msg=messageQueue.shift();
try{processMessage(msg)}catch(e){}
isProcessing=false;
if(messageQueue.length)requestAnimationFrame(processQueue);
}

var ws,ra=0,pingInterval;
function conn(){
var pr=location.protocol==="https:"?"wss:":"ws:";
ws=new WebSocket(pr+"//"+location.host+"/ws");
ws.binaryType='arraybuffer';
ws.onopen=function(){
ra=0;
if(pingInterval)clearInterval(pingInterval);
pingInterval=setInterval(function(){
if(ws&&ws.readyState===1)try{ws.send('ping')}catch(e){}
},25000);
};
ws.onmessage=function(e){
try{
var d;
if(e.data instanceof ArrayBuffer){
d=JSON.parse(new TextDecoder().decode(e.data));
}else{
d=JSON.parse(e.data);
}
messageQueue.push(d);
requestAnimationFrame(processQueue);
}catch(x){}
};
ws.onclose=function(){
if(pingInterval)clearInterval(pingInterval);
ra++;
setTimeout(conn,Math.min(1000*Math.pow(1.3,ra-1),15000));
};
ws.onerror=function(){};
}
conn();

function updateJam(){
var n=new Date();
var tgl=n.toLocaleDateString('id-ID',{day:'2-digit',month:'long',year:'numeric'});
var jam=n.toLocaleTimeString('id-ID',{hour12:false});
document.getElementById("jam").textContent=tgl+" "+jam+" WIB ";
}
setInterval(updateJam,1000);
updateJam();

window.toggleTheme=function(){
var b=document.body,btn=document.getElementById('themeBtn');
b.classList.toggle('dark-mode');
isDark=b.classList.contains('dark-mode');
btn.textContent=isDark?"☀️":"🌙";
localStorage.setItem('theme',isDark?'dark':'light');
document.getElementById('tradingview_chart').innerHTML='';
createTradingViewWidget();
};

if(localStorage.getItem('theme')==='dark'){
document.body.classList.add('dark-mode');
document.getElementById('themeBtn').textContent="☀️";
}

setTimeout(createTradingViewWidget,100);
})();
</script>
</body>
</html>"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(api_loop()),
        asyncio.create_task(usd_idr_loop()),
        asyncio.create_task(heartbeat_loop())
    ]
    await start_telegram_bot()
    yield
    for t in tasks:
        t.cancel()
    await stop_telegram_bot()
    await close_aiohttp_session()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Gold Monitor", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML_TEMPLATE)


@app.get("/api/state")
async def get_state():
    return Response(
        content=await state_cache.get_state_bytes(),
        media_type="application/json"
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    if not await manager.connect(ws):
        await ws.close(code=1013, reason="Too many connections")
        return
    try:
        initial_data = await state_cache.get_state_bytes()
        await ws.send_bytes(initial_data)
        while True:
            try:
                data = await asyncio.wait_for(ws.receive(), timeout=45.0)
                msg_type = data.get("type")
                if msg_type == "websocket.disconnect":
                    break
                if data.get("text") == "ping" or data.get("bytes") == b"ping":
                    await ws.send_bytes(b'{"pong":true}')
            except asyncio.TimeoutError:
                try:
                    await ws.send_bytes(b'{"ping":true}')
                except:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.disconnect(ws)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=20,
        limit_concurrency=500,
        backlog=256,
        timeout_keep_alive=30,
    )
