"""Registro de los Mejores props NFL (y Anotador TD) y su resultado real.

Igual que nba/picks.py: cada actualización guarda los picks de los partidos que todavía no empiezan
(la última foto antes de cada partido manda) y los califica con los stats por semana de nflverse.
Todo queda en nfl/data/picks.json.
"""
import datetime, json, math, os, subprocess
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
PICKS_JSON = os.path.join(HERE, "data", "picks.json")
COLS = {"cmp": ["completions"], "att": ["attempts"], "pyd": ["passing_yards"], "ptd": ["passing_tds"],
        "int": ["passing_interceptions"], "car": ["carries"], "ryd": ["rushing_yards"], "tgt": ["targets"],
        "rec": ["receptions"], "recyd": ["receiving_yards"], "rr": ["rushing_yards", "receiving_yards"],
        "td": ["rushing_tds", "receiving_tds"]}
ET_OFFSET = datetime.timedelta(hours=5)  # noviembre-enero: hora del este = UTC-5


def snapshot(html_path):
    env = dict(os.environ)
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
    if root:
        env["NODE_PATH"] = root + (os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else "")
    r = subprocess.run(["node", os.path.join(HERE, "picks_snapshot.js"), html_path],
                       capture_output=True, text=True, env=env, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300] or "node falló")
    out = json.loads(r.stdout)
    if out.get("errors"):
        raise RuntimeError("; ".join(out["errors"])[:300])
    return out


def game_start_utc(date, hhmm):
    h, m = (hhmm or "13:00").split(":")
    return datetime.datetime.strptime(date, "%Y-%m-%d").replace(hour=int(h), minute=int(m)) + ET_OFFSET


def load():
    if os.path.exists(PICKS_JSON):
        with open(PICKS_JSON) as f:
            return json.load(f)
    return {"picks": []}


def save(db):
    with open(PICKS_JSON, "w") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))


def merge(db, snap, taken_at):
    """Guarda los picks de los partidos que empiezan después de taken_at (UTC). Por partido manda la foto más reciente."""
    stamp = taken_at.strftime("%Y-%m-%dT%H:%MZ")
    games = {}
    for d, t, away, home, wk in snap["sched"]:
        if game_start_utc(d, t) > taken_at:
            games[(d, away)] = games[(d, home)] = (d, away, home)
    if not games:
        return 0
    covered = set(games.values())
    keep = [p for p in db["picks"] if not (games.get((p["d"], p["tm"])) in covered and p.get("t", "") <= stamp)]
    b = snap.get("b") or 1.0
    added = 0
    for d, rows in snap["picks"].items():
        for p in rows:
            if (d, p["tm"]) in games:
                p["t"] = stamp
                if p["k"] != "td":
                    p["raw"] = round(1 / (1 + math.exp(-math.log(p["prob"] / (1 - p["prob"])) / b)), 4)
                keep.append(p)
                added += 1
    db["picks"] = keep
    return added


def grade(db, stats_csv, snaps_csv=None):
    """Califica con los stats por semana de nflverse. Si jugó snaps pero no registró nada, su stat es 0;
    si no jugó, el pick se anula."""
    st = pd.read_csv(stats_csv, low_memory=False)
    st = st[st.season_type == "REG"]
    played = set(zip(st.team, st.week))
    idx = {(r.player_display_name, r.team, int(r.week)): r for r in st.itertuples()}
    snapped = set()
    if snaps_csv and os.path.exists(snaps_csv):
        sn = pd.read_csv(snaps_csv, low_memory=False)
        snapped = {(r.player, r.team, int(r.week)) for r in sn[sn.offense_snaps > 0].itertuples()}
    n = 0
    for p in db["picks"]:
        if p.get("res") or p.get("wk") is None:
            continue
        wk = int(p["wk"])
        if (p["tm"], wk) not in played:
            continue
        r = idx.get((p["n"], p["tm"], wk))
        if r is None and (p["n"], p["tm"], wk) in snapped:
            p["act"] = 0
            p["res"] = "P" if p["line"] == 0 else ("L" if p["side"] == "O" else "W")
        elif r is None:
            p["res"] = "V"
        else:
            act = float(sum(float(getattr(r, c) or 0) for c in COLS[p["k"]]))
            act = int(act) if act.is_integer() else round(act, 1)
            p["act"] = act
            p["res"] = "P" if act == p["line"] else ("W" if (act > p["line"]) == (p["side"] == "O") else "L")
        n += 1
    return n


def fit_shrink(db, prior=300):
    """Igual que en la NBA: probabilidad real = sigmoide(b × logit(prob. del modelo)), con b medido en los picks calificados."""
    rows = [(p.get("raw", p["prob"]), p["res"] == "W") for p in db["picks"]
            if p["k"] != "td" and p.get("res") in ("W", "L") and 0 < p.get("raw", p["prob"]) < 1]
    if len(rows) < 100:
        return {"b": 1.0, "n": len(rows)}
    best = None
    for i in range(30, 121):
        b = i / 100
        ll = sum(math.log(q if w else 1 - q) for p, w in rows for q in [1 / (1 + math.exp(-b * math.log(p / (1 - p))))])
        if best is None or ll > best[1]:
            best = (b, ll)
    b = (len(rows) * best[0] + prior) / (len(rows) + prior)
    return {"b": round(b, 3), "fit": best[0], "n": len(rows)}


def compact(db):
    out = []
    for p in sorted(db["picks"], key=lambda x: (x["d"], -x["prob"])):
        if p.get("res") in ("W", "L", "P"):
            out.append([p["d"], p["wk"], p["n"], p["tm"], p["pos"], p["opp"], p["k"], p["line"], p["side"],
                        round(p["prob"], 3), round(p["edge"], 3), p["res"], p.get("act")])
    return out


def pending(db):
    return sum(1 for p in db["picks"] if not p.get("res"))
