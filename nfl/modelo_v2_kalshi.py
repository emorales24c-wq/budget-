"""Prueba de la regla Under (NFL) contra precios reales de Kalshi, en partidos ya jugados de 2026.

Regla (fija, igual que modelo_v2_backtest.py): en un prop, si el promedio de los últimos 5 partidos
queda por debajo del promedio de temporada (usando solo partidos anteriores), se juega Under
a la línea de temporada redondeada a .5. Solo cuenta si Kalshi tiene esa línea exacta.
Precio: el de SÍ antes del partido (cierre); Under = comprar NO a (1 − bid), con comisión de Kalshi.
Kickoff aproximado: hora del juego de games.csv (ET, UTC−5).
"""
import calendar, datetime, json, math, re, subprocess, time, unicodedata
import pandas as pd

API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"KXNFLRECYDS": "recyd", "KXNFLREC": "rec", "KXNFLRSHYDS": "ryd"}
MESES = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’\-]", "", s)
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()

def get(url):
    for i in range(5):
        r = subprocess.run(["curl", "-sS", "-m", "30", url], capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
            if isinstance(d, dict) and ("markets" in d or "candlesticks" in d):
                return d
        except ValueError:
            pass
        time.sleep(2 * (i + 1))
    return None

def settled(series):
    out, cur = [], ""
    while True:
        d = get(f"{API}/markets?limit=1000&status=settled&series_ticker={series}" + (f"&cursor={cur}" if cur else ""))
        time.sleep(1.0)
        if not d:
            break
        out += d["markets"]
        cur = d.get("cursor") or ""
        if not cur:
            break
    return out

def closing_yes(ticker, start_utc):
    series = ticker.split("-")[0]
    end = calendar.timegm(start_utc.timetuple())
    d = get(f"{API}/series/{series}/markets/{ticker}/candlesticks?start_ts={end-3600}&end_ts={end}&period_interval=1")
    time.sleep(0.4)
    if not d:
        return None
    for c in reversed(d.get("candlesticks", [])):
        try:
            bid = float(c["yes_bid"]["close_dollars"]); ask = float(c["yes_ask"]["close_dollars"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < ask < 1 and bid > 0:
            return bid, ask
    return None

def kfee(c):
    return math.ceil(0.07 * c * (1 - c) * 100) / 100

if __name__ == "__main__":
    import sys
    st = pd.read_csv("nfl/data/raw/stats_player_week_2026.csv", low_memory=False)
    st = st[st.season_type == "REG"].sort_values(["player_id", "week"])
    gm = pd.read_csv("nfl/data/raw/games.csv", low_memory=False)
    gm = gm[(gm.season == 2026) & (gm.game_type == "REG")][["game_id", "gameday", "gametime", "week"]]
    gdate = dict(zip(gm.game_id, gm.gameday)); gtime = dict(zip(gm.game_id, gm.gametime))
    # historial por jugador y prop
    logs = {}
    for (pid, nm), g in st.groupby(["player_id", "player_display_name"]):
        logs[norm(nm)] = g.reset_index(drop=True)
    COL = {"recyd": "receiving_yards", "rec": "receptions", "ryd": "rushing_yards"}
    cands = []
    for series, lab in SERIES.items():
        for m in settled(series):
            title = m.get("title") or ""
            if ":" not in title or m.get("strike_type") != "greater" or m.get("floor_strike") is None:
                continue
            name = norm(title.split(":")[0])
            g = logs.get(name)
            if g is None:
                continue
            ev = m.get("event_ticker", "")
            mm = re.search(r"-(\d\d)([A-Z]{3})(\d\d)", ev)
            if not mm:
                continue
            date = f"20{mm.group(1)}-{MESES[mm.group(2)]:02d}-{int(mm.group(3)):02d}"
            rows = g[g.game_id.map(lambda x: gdate.get(x)) == date]
            if rows.empty:
                continue
            wk = int(rows.week.iloc[0])
            prev = g[g.week < wk][COL[lab]].values
            if len(prev) < 5:
                continue
            L0 = round(prev.mean() * 2) / 2
            if L0 != float(m["floor_strike"]):
                continue
            dev = prev[-5:].mean() / L0 - 1
            if dev > 0:
                continue
            gid = rows.game_id.iloc[0]
            start = datetime.datetime.strptime(f"{date} {gtime.get(gid) or '13:00'}", "%Y-%m-%d %H:%M") + datetime.timedelta(hours=5)
            actual = float(rows[COL[lab]].iloc[0])
            cands.append(dict(ticker=m["ticker"], name=name, stat=lab, line=L0, week=wk, start=start,
                              result=m.get("result"), actual=actual))
    print(f"candidatos (Under con línea exacta de Kalshi): {len(cands)}", flush=True)
    res = []
    for c in cands:
        q = closing_yes(c["ticker"], c["start"])
        if not q:
            continue
        bid, ask = q
        cost = round(1 - bid, 2)
        if cost >= 1 or cost <= 0:
            continue
        cost_f = cost + kfee(cost)
        win = c["result"] == "no"  # Under = NO
        pnl = ((1 - cost_f) / cost_f) if win else -1
        res.append(dict(c, cost=cost, win=win, pnl=pnl))
    df = pd.DataFrame(res)
    df.to_csv("/tmp/claude-0/-home-user-budget-/d0a99350-63fe-5aee-a3c5-cee0e014777d/scratchpad/kalshi_under_test.csv", index=False)
    if df.empty:
        print("sin apuestas con precio"); sys.exit()
    for lab, s in [("todas", df), ("entrena sem<=8", df[df.week <= 8]), ("prueba sem>=9", df[df.week >= 9])]:
        if len(s):
            print(f"{lab}: n={len(s)} aciertos={s.win.mean()*100:.1f}% precio prom={s.cost.mean()*100:.0f}¢ ROI={s.pnl.mean()*100:.1f}%")
    print(df.groupby("stat").agg(n=("win", "size"), acierto=("win", "mean"), roi=("pnl", "mean")).round(3).to_string())
