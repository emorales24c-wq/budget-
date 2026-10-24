"""Descarga los stats NFL (nflverse) y regenera el dashboard de props NFL.

Uso:  python3 nfl/actualizar_nfl.py
Fuentes (datos abiertos de nflverse en GitHub):
  - stats por jugador y semana: nflverse-data, release "stats_player"
  - calendario, marcadores y líneas: nfldata/data/games.csv
"""
import datetime, json, os, subprocess, sys, time
import pandas as pd

SEASON = 2026
HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw")
JSON_OUT = os.path.join(HERE, "data", "nfl_data.json")
HTML_TPL = os.path.join(HERE, "plantilla_nfl.html")
HTML_OUT = os.path.join(HERE, "proyecciones-nfl.html")
HOY = datetime.date.today()
FECHA = f"{HOY.day}-{['ene','feb','mar','abr','may','jun','jul','ago','sep','oct','nov','dic'][HOY.month-1]}-{HOY.year}"

URL_STATS = f"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{SEASON}.csv"
URL_GAMES = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

SHRINK_GAMES = 3  # con pocos partidos, el factor de defensa se acerca a 1.00
SHRINK_RARE = 8   # touchdowns e intercepciones son raros: se encogen más
RARE = {"ptd", "int", "rtd", "rectd"}
POS = ["QB", "RB", "WR", "TE"]
# stats por partido que usa el modelo (columnas de nflverse)
STATS = [("cmp", "completions"), ("att", "attempts"), ("pyd", "passing_yards"), ("ptd", "passing_tds"),
         ("int", "passing_interceptions"), ("car", "carries"), ("ryd", "rushing_yards"), ("rtd", "rushing_tds"),
         ("tgt", "targets"), ("rec", "receptions"), ("recyd", "receiving_yards"), ("rectd", "receiving_tds")]
TEAMS = {"ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
         "CAR": "Carolina Panthers", "CHI": "Chicago Bears", "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
         "DAL": "Dallas Cowboys", "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
         "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
         "LA": "Los Angeles Rams", "LAC": "Los Angeles Chargers", "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins",
         "MIN": "Minnesota Vikings", "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
         "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
         "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers", "TEN": "Tennessee Titans", "WAS": "Washington Commanders"}


def fetch(url, path):
    for intento in range(4):
        r = subprocess.run(["curl", "-sSL", "-f", "-m", "120", "-o", path, url])
        if r.returncode == 0:
            return
        time.sleep(2 ** (intento + 1))
    sys.exit(f"No se pudo descargar {url}")


os.makedirs(RAW, exist_ok=True)
fetch(URL_STATS, os.path.join(RAW, f"stats_player_week_{SEASON}.csv"))
fetch(URL_GAMES, os.path.join(RAW, "games.csv"))

st = pd.read_csv(os.path.join(RAW, f"stats_player_week_{SEASON}.csv"), low_memory=False)
st = st[st.season_type == "REG"].copy()
st["pos"] = st.position.replace({"FB": "RB"})
st = st[st.pos.isin(POS)]
for _, col in STATS:
    st[col] = pd.to_numeric(st[col], errors="coerce").fillna(0)

games = pd.read_csv(os.path.join(RAW, "games.csv"), low_memory=False)
games = games[(games.season == SEASON) & (games.game_type == "REG")]
done = games[games.home_score.notna()]

# Equipos: puntos anotados y permitidos por partido
team = {a: {"pf": 0.0, "pa": 0.0, "n": 0} for a in TEAMS}
for _, g in done.iterrows():
    for me, op, pf, pa in ((g.home_team, g.away_team, g.home_score, g.away_score), (g.away_team, g.home_team, g.away_score, g.home_score)):
        team[me]["pf"] += pf; team[me]["pa"] += pa; team[me]["n"] += 1
T = [[a, TEAMS[a], round(v["pf"] / v["n"], 2) if v["n"] else 0, round(v["pa"] / v["n"], 2) if v["n"] else 0, v["n"]] for a, v in sorted(team.items())]

# Próxima semana: partidos sin marcador
pend = games[games.home_score.isna()]
NEXT = []
if len(pend):
    wk = int(pend.week.min())
    for _, g in pend[pend.week == wk].sort_values(["gameday", "gametime"]).iterrows():
        f = lambda v: None if pd.isna(v) else float(v)
        NEXT.append([wk, g.gameday, g.gametime, g.away_team, g.home_team, f(g.spread_line), f(g.total_line)])

# Jugadores: promedio por partido jugado; equipo = el de su último partido
PL = []
for pid, d in st.sort_values("week").groupby("player_id"):
    last = d.iloc[-1]
    n = len(d)
    avg = [round(d[col].sum() / n, 2) for _, col in STATS]
    PL.append([last.player_display_name, last.team, last.pos, n, int(last.week)] + avg)
PL.sort(key=lambda r: (r[1], POS.index(r[2]), -r[5 + 2] - r[5 + 6] - r[5 + 10]))

# Defensa por posición: lo que cada defensa permite por partido a QB/RB/WR/TE del rival
allowed = {a: {"n": 0, **{p: [0.0] * len(STATS) for p in POS}} for a in TEAMS}
for gid, d in st.groupby("game_id"):
    for de, offd in d.groupby("opponent_team"):
        if de not in allowed:
            continue
        allowed[de]["n"] += 1
        for p, dp in offd.groupby("pos"):
            allowed[de][p] = [a + b for a, b in zip(allowed[de][p], [dp[col].sum() for _, col in STATS])]
tg = sum(v["n"] for v in allowed.values())
league = {p: [sum(v[p][k] for v in allowed.values()) / tg for k in range(len(STATS))] for p in POS}
DVP = {}
for a, v in allowed.items():
    n = v["n"]; row = {"n": n}
    for p in POS:
        pg = [round(x / n, 2) if n else 0 for x in v[p]]
        fac = []
        for k in range(len(STATS)):
            raw = (pg[k] / league[p][k]) if league[p][k] and n else 1.0
            k_sh = SHRINK_RARE if STATS[k][0] in RARE else SHRINK_GAMES
            fac.append(round((n * raw + k_sh) / (n + k_sh), 3))
        row[p] = pg + fac
    DVP[a] = row

data = {"fecha": FECHA, "season": SEASON, "semanas": int(st.week.max()), "stats": [k for k, _ in STATS],
        "t": T, "p": PL, "dvp": DVP, "next": NEXT, "shrink": SHRINK_GAMES}
with open(JSON_OUT, "w") as f:
    json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
with open(HTML_TPL) as f:
    tpl = f.read()
with open(HTML_OUT, "w") as f:
    f.write(tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":"))).replace("__FECHA__", FECHA).replace("__SEMANAS__", str(data["semanas"])).replace("__SHRINK__", f"{SHRINK_GAMES} ({SHRINK_RARE} para touchdowns e intercepciones)"))
print(f"Listo ({FECHA}): semanas 1-{data['semanas']}, {len(PL)} jugadores, {len(done)} partidos, {len(NEXT)} partidos próximos")
print(" ", HTML_OUT)
