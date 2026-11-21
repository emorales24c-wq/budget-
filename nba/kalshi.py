"""Precios de Kalshi para props de jugadores (datos públicos, sin clave).

Cada mercado de Kalshi es un escalón: "Jamal Shead: 15+ points" paga $1 si anota 15 o más.
Comprar SÍ al precio ask = Over 14.5; comprar NO a (1 − bid) = Under 14.5.
Devuelve {nombre normalizado: {stat: [[fecha, línea, bid, ask], ...]}} para que el dashboard
compare la probabilidad del modelo con el precio real.
"""
import json, re, subprocess, time, unicodedata

API = "https://api.elections.kalshi.com/trade-api/v2/markets"
NBA = {"pts": "KXNBAPTS", "reb": "KXNBAREB", "ast": "KXNBAAST", "tpm": "KXNBA3PT",
       "stl": "KXNBASTL", "blk": "KXNBABLK", "pra": "KXNBAPRA"}
NFL = {"pyd": "KXNFLPASSYDS", "cmp": "KXNFLPASSCOMP", "att": "KXNFLPASSATT", "ptd": "KXNFLPASSTDS",
       "int": "KXNFLPASSINT", "ryd": "KXNFLRSHYDS", "car": "KXNFLRSHATT", "rec": "KXNFLREC",
       "recyd": "KXNFLRECYDS", "rr": "KXNFLRRYDS", "td": "KXNFLTD"}
MESES = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def norm(name):
    """Nombre comparable entre fuentes: sin acentos, sin puntos ni sufijos (Jr., III...)."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’\-]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(url):
    for intento in range(5):
        r = subprocess.run(["curl", "-sS", "-m", "30", url], capture_output=True, text=True)
        if r.returncode == 0:
            try:
                d = json.loads(r.stdout)
            except ValueError:
                d = {}
            if "markets" in d:
                return d
        time.sleep(2 * (intento + 1))
    return None


def _date(event_ticker):
    m = re.search(r"-(\d\d)([A-Z]{3})(\d\d)", event_ticker)
    if not m:
        return None
    return f"20{m.group(1)}-{MESES[m.group(2)]:02d}-{int(m.group(3)):02d}"


def fetch(series):
    """series: {stat: ticker de la serie}. Devuelve (precios, n_mercados, fallidas)."""
    out, n, failed = {}, 0, []
    for k, s in series.items():
        cursor = ""
        while True:
            d = _get(f"{API}?limit=1000&status=open&series_ticker={s}" + (f"&cursor={cursor}" if cursor else ""))
            time.sleep(1.2)  # Kalshi limita los pedidos por segundo
            if d is None:
                failed.append(s)
                break
            for m in d["markets"]:
                title = m.get("title") or ""
                if ":" not in title or m.get("strike_type") != "greater" or m.get("floor_strike") is None:
                    continue
                try:
                    bid = float(m.get("yes_bid_dollars") or 0)
                    ask = float(m.get("yes_ask_dollars") or 0)
                except ValueError:
                    continue
                if ask <= 0 or ask >= 1:
                    continue  # sin vendedores: no se puede comprar
                date = _date(m.get("event_ticker", ""))
                out.setdefault(norm(title.split(":")[0]), {}).setdefault(k, []).append(
                    [date, float(m["floor_strike"]), round(bid, 2), round(ask, 2), m.get("ticker", "")])
                n += 1
            cursor = d.get("cursor") or ""
            if not cursor:
                break
    for v in out.values():
        for rows in v.values():
            rows.sort(key=lambda r: (r[0] or "", r[1]))
    return out, n, failed


def closing_mid(ticker, start_utc):
    """(bid, ask) de SÍ en el último minuto con precio antes de start_utc (datetime UTC sin zona)."""
    import calendar
    series = ticker.split("-")[0]
    end = calendar.timegm(start_utc.timetuple())
    url = (f"https://api.elections.kalshi.com/trade-api/v2/series/{series}/markets/{ticker}/candlesticks"
           f"?start_ts={end - 3600}&end_ts={end}&period_interval=1")
    for intento in range(4):
        r = subprocess.run(["curl", "-sS", "-m", "30", url], capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
        except ValueError:
            d = {}
        if "candlesticks" in d:
            for c in reversed(d["candlesticks"]):
                try:
                    bid = float(c["yes_bid"]["close_dollars"]); ask = float(c["yes_ask"]["close_dollars"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 < ask < 1 and bid > 0:
                    return round(bid, 3), round(ask, 3)
            return None
        time.sleep(2 * (intento + 1))
    return None


def add_clv(picks, start_of, now_utc, limit=400):
    """Para los picks de Kalshi cuyo partido ya empezó: precio de cierre y si le ganamos (CLV).
    start_of(p) -> datetime UTC del inicio del partido o None."""
    n = 0
    for p in picks:
        if n >= limit:
            break
        if p.get("src") != "k" or not p.get("tk") or "close" in p or p.get("c") is None:
            continue
        st = start_of(p)
        if not st or st > now_utc:
            continue
        mid = closing_mid(p["tk"], st)
        time.sleep(0.4)
        n += 1
        if mid is None:
            p["close"] = None
            continue
        # Over = compramos SÍ a c; Under = compramos NO a c. Lo que costaba ese mismo lado al cierre:
        bid, ask = mid
        close_cost = ask if p["side"] == "O" else 1 - bid
        p["close"] = round(close_cost, 3)
        p["clv"] = round(close_cost - p["c"], 3)   # + = al cierre costaba más: le ganamos al mercado
    return n
