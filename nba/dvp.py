"""Defensa por posición (DvP) a partir de los box scores de basketball-reference.

Cada box score se guarda una vez en nba/data/boxscores/, así que cada actualización
solo descarga los partidos nuevos.
"""
import datetime, json, os, re, subprocess, time
from io import StringIO
import pandas as pd

POS = ["PG", "SG", "SF", "PF", "C"]
STATS = ["pts", "reb", "ast", "tpm", "stl", "blk"]
SHRINK_GAMES = 5  # con pocos partidos, el factor se acerca a 1.00 (promedio de liga)
MONTHS = ["october", "november", "december", "january", "february", "march", "april", "may", "june"]
BBREF = "https://www.basketball-reference.com"


def _get(url, path=None):
    for intento in range(4):
        r = subprocess.run(["curl", "-sS", "-f", "-m", "60", "-A", "Mozilla/5.0", url], capture_output=True)
        if r.returncode == 0:
            html = r.stdout.decode("utf-8", "replace")
            if path:
                with open(path, "w") as f:
                    f.write(html)
            return html
        time.sleep(2 ** (intento + 1))
    return None


def _mp(v):
    m = re.match(r"^(\d+):(\d+)$", str(v))
    return int(m.group(1)) + int(m.group(2)) / 60 if m else None


def _parse_box(html, gid):
    teams = re.findall(r'id="box-([A-Z]{3})-game-basic"', html)
    out = {"id": gid, "teams": {}}
    for tm in teams:
        t = pd.read_html(StringIO(html), attrs={"id": f"box-{tm}-game-basic"})[0]
        t.columns = [c[1] for c in t.columns]
        players, poss = [], None
        for _, r in t.iterrows():
            name = str(r["Starters"])
            if name == "Team Totals":
                fga, fta, orb, tov = (float(r[k]) for k in ("FGA", "FTA", "ORB", "TOV"))
                poss = fga + 0.44 * fta - orb + tov
                continue
            mp = _mp(r["MP"])
            if not mp:
                continue
            f = lambda k: float(r[k]) if str(r[k]) not in ("", "nan") else 0.0
            players.append([name, round(mp, 2), f("PTS"), f("TRB"), f("AST"), f("3P"), f("STL"), f("BLK")])
        out["teams"][tm] = {"poss": poss, "players": players}
    return out


def update_boxscores(season_end, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    today = datetime.date.today()
    ids = []
    for i, m in enumerate(MONTHS):
        year = season_end - 1 if i < 3 else season_end
        month_num = [10, 11, 12, 1, 2, 3, 4, 5, 6][i]
        if datetime.date(year, month_num, 1) > today:
            break
        html = _get(f"{BBREF}/leagues/NBA_{season_end}_games-{m}.html")
        time.sleep(3.5)  # basketball-reference permite ~20 pedidos por minuto
        if html:
            ids += re.findall(r"/boxscores/(\d{9}[A-Z]{3})\.html", html)
    nuevos = 0
    for gid in dict.fromkeys(ids):
        path = os.path.join(cache_dir, gid + ".json")
        if os.path.exists(path):
            continue
        html = _get(f"{BBREF}/boxscores/{gid}.html")
        time.sleep(3.5)
        if not html:
            print(f"  aviso: no se pudo bajar el box score {gid}")
            continue
        with open(path, "w") as f:
            json.dump(_parse_box(html, gid), f, ensure_ascii=False, separators=(",", ":"))
        nuevos += 1
    return nuevos


def compute_dvp(cache_dir, positions):
    """positions: {nombre jugador: 'PG'|'SG'|...}. Devuelve {equipo: {'n': partidos, POS: [6 por partido + 6 factores]}}."""
    allowed = {}  # defensa -> pos -> stat totals, y posesiones
    for fn in sorted(os.listdir(cache_dir)):
        g = json.load(open(os.path.join(cache_dir, fn)))
        tms = list(g["teams"])
        if len(tms) != 2:
            continue
        poss = [g["teams"][t]["poss"] for t in tms]
        if None in poss:
            continue
        game_poss = sum(poss) / 2
        for off, de in ((tms[0], tms[1]), (tms[1], tms[0])):
            d = allowed.setdefault(de, {"n": 0, "poss": 0.0, **{p: [0.0] * 6 for p in POS}})
            d["n"] += 1
            d["poss"] += game_poss
            for name, mp, *vals in g["teams"][off]["players"]:
                pos = positions.get(name)
                if pos in POS:
                    d[pos] = [a + b for a, b in zip(d[pos], vals)]
    if not allowed:
        return {}
    tot_poss = sum(d["poss"] for d in allowed.values())
    league = {p: [sum(d[p][k] for d in allowed.values()) / tot_poss for k in range(6)] for p in POS}
    res = {}
    for tm, d in allowed.items():
        row = {"n": d["n"]}
        for p in POS:
            per_game = [round(v / d["n"], 1) for v in d[p]]
            facs = []
            for k in range(6):
                raw = (d[p][k] / d["poss"]) / league[p][k] if league[p][k] else 1.0
                facs.append(round((d["n"] * raw + SHRINK_GAMES) / (d["n"] + SHRINK_GAMES), 3))
            row[p] = per_game + facs
        res[tm] = row
    return res


def main_position(pos):
    pos = str(pos or "")
    return pos.split("-")[0] if pos else ""
