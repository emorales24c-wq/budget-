"""Defensa por posición (DvP) a partir de los box scores de basketball-reference.

Cada box score se guarda una vez en nba/data/boxscores/, así que cada actualización
solo descarga los partidos nuevos.

Se separa titulares (S) y suplentes (B) y se mide por minuto: lo que un equipo permite
a los bases se reparte entre todos los bases que juegan, no solo el titular.
"""
import datetime, json, os, re, subprocess, time
from io import StringIO
import pandas as pd

POS = ["PG", "SG", "SF", "PF", "C"]
STATS = ["pts", "reb", "ast", "tpm", "stl", "blk"]
ROLES = ["S", "B"]  # titulares, suplentes
BOX_VERSION = 2
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
    out = {"id": gid, "v": BOX_VERSION, "teams": {}}
    for tm in teams:
        t = pd.read_html(StringIO(html), attrs={"id": f"box-{tm}-game-basic"})[0]
        t.columns = [c[1] for c in t.columns]
        players, poss, starter = [], None, True
        for _, r in t.iterrows():
            name = str(r["Starters"])
            if name == "Reserves":
                starter = False
                continue
            if name == "Team Totals":
                fga, fta, orb, tov = (float(r[k]) for k in ("FGA", "FTA", "ORB", "TOV"))
                poss = fga + 0.44 * fta - orb + tov
                continue
            mp = _mp(r["MP"])
            if not mp:
                continue
            f = lambda k: float(r[k]) if str(r[k]) not in ("", "nan") else 0.0
            players.append([name, "S" if starter else "B", round(mp, 2), f("PTS"), f("TRB"), f("AST"), f("3P"), f("STL"), f("BLK")])
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
            try:
                if json.load(open(path)).get("v") == BOX_VERSION:
                    continue
            except ValueError:
                pass
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
    """positions: {nombre: 'PG'|...}.
    Devuelve {equipo: {'n': partidos, 'S'|'B': {POS: [6 por 36 min, 6 factores, min por partido]}}}."""
    allowed = {}  # defensa -> rol -> pos -> [min, 6 stats]
    for fn in sorted(os.listdir(cache_dir)):
        g = json.load(open(os.path.join(cache_dir, fn)))
        tms = list(g["teams"])
        if len(tms) != 2 or g.get("v") != BOX_VERSION:
            continue
        for off, de in ((tms[0], tms[1]), (tms[1], tms[0])):
            d = allowed.setdefault(de, {"n": 0, **{ro: {p: [0.0] * 7 for p in POS} for ro in ROLES}})
            d["n"] += 1
            for name, role, mp, *vals in g["teams"][off]["players"]:
                pos = positions.get(name)
                if pos in POS:
                    d[role][pos] = [a + b for a, b in zip(d[role][pos], [mp] + vals)]
    if not allowed:
        return {}
    games = sum(d["n"] for d in allowed.values())
    league = {}
    for ro in ROLES:
        for p in POS:
            tot = [sum(d[ro][p][k] for d in allowed.values()) for k in range(7)]
            league[ro, p] = {"rate": [tot[k + 1] / tot[0] if tot[0] else 0 for k in range(6)],
                             "min_pg": tot[0] / games}
    res = {}
    for tm, d in allowed.items():
        row = {"n": d["n"]}
        for ro in ROLES:
            row[ro] = {}
            for p in POS:
                mins, vals = d[ro][p][0], d[ro][p][1:]
                lg = league[ro, p]
                k_min = SHRINK_GAMES * lg["min_pg"]  # el encogimiento se mide en minutos
                per36, facs = [], []
                for k in range(6):
                    rate = vals[k] / mins if mins else 0
                    per36.append(round(rate * 36, 1))
                    raw = rate / lg["rate"][k] if lg["rate"][k] else 1.0
                    facs.append(round((mins * raw + k_min) / (mins + k_min), 3) if mins + k_min else 1.0)
                row[ro][p] = per36 + facs + [round(mins / d["n"], 1)]
        res[tm] = row
    return res


def main_position(pos):
    pos = str(pos or "")
    return pos.split("-")[0] if pos else ""
