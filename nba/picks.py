"""Registro de los Mejores props del modelo y su resultado real.

Cada actualización guarda los props que el dashboard recomienda para los partidos que todavía no
empiezan (si un partido ya se guardó antes, se reemplaza por la foto más reciente antes de empezar).
Después se califica cada pick con el box score de basketball-reference: ganado, perdido o anulado
(el jugador no jugó). Todo queda en nba/data/picks.json.
"""
import datetime, json, math, os, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PICKS_JSON = os.path.join(HERE, "data", "picks.json")
STAT_IX = {"pts": [3], "reb": [4], "ast": [5], "tpm": [6], "stl": [7], "blk": [8],
           "pra": [3, 4, 5], "pr": [3, 4], "pa": [3, 5], "ra": [4, 5]}
ET_OFFSET = datetime.timedelta(hours=5)  # noviembre-marzo: hora del este = UTC-5


def snapshot(html_path):
    """Corre el dashboard sin ventana y devuelve {'sched': [...], 'picks': {fecha: [pick, ...]}}."""
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


def game_start_utc(date, time_str):
    """'2026-11-11', '7:30p' -> datetime UTC del inicio."""
    t = (time_str or "7:00p").strip().lower()
    pm = t.endswith("p")
    hh, mm = t.rstrip("ap").split(":")
    h = int(hh) % 12 + (12 if pm else 0)
    d = datetime.datetime.strptime(date, "%Y-%m-%d")
    return d.replace(hour=h, minute=int(mm)) + ET_OFFSET


def load():
    if os.path.exists(PICKS_JSON):
        with open(PICKS_JSON) as f:
            return json.load(f)
    return {"picks": []}


def save(db):
    with open(PICKS_JSON, "w") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"))


def merge(db, snap, taken_at, cal=1):
    """Guarda los picks de los partidos que empiezan después de taken_at (datetime UTC).
    Por partido manda la foto más reciente antes de que empiece."""
    stamp = taken_at.strftime("%Y-%m-%dT%H:%MZ")
    games, starts = {}, {}
    for g in snap["sched"]:
        start = game_start_utc(g[0], g[1])
        if start > taken_at:
            games[(g[0], g[2])] = games[(g[0], g[3])] = (g[0], g[2], g[3])
            starts[(g[0], g[2])] = starts[(g[0], g[3])] = start.strftime("%Y-%m-%dT%H:%MZ")
    if not games:
        return 0
    covered = set(games.values())
    keep = []
    for p in db["picks"]:
        gk = games.get((p["d"], p["tm"]))
        if gk in covered and p.get("t", "") <= stamp:
            continue  # se reemplaza por la foto nueva
        keep.append(p)
    added = 0
    for d, rows in snap["picks"].items():
        for p in rows:
            if (d, p["tm"]) in games:
                p["t"] = stamp
                p["gs"] = starts.get((d, p["tm"]))
                b = snap.get("b") or 1.0  # corrección que tenía la página: guarda también la probabilidad sin corregir
                p["raw"] = round(1 / (1 + math.exp(-math.log(p["prob"] / (1 - p["prob"])) / b)), 4)
                p["cal"] = cal  # 1 = con el modelo calibrado (desde el 4-nov-2026)
                keep.append(p)
                added += 1
    db["picks"] = keep
    return added


def _box_index(cache_dir):
    idx = {}
    for fn in os.listdir(cache_dir):
        if not fn.endswith(".json"):
            continue
        g = json.load(open(os.path.join(cache_dir, fn)))
        date = f"{fn[0:4]}-{fn[4:6]}-{fn[6:8]}"
        for tm, side in g.get("teams", {}).items():
            idx[(date, tm)] = {pl[0]: pl for pl in side["players"]}
    return idx


def grade(db, cache_dir):
    """Califica los picks cuyo box score ya está guardado. Devuelve cuántos se calificaron ahora."""
    idx = _box_index(cache_dir)
    n = 0
    for p in db["picks"]:
        if p.get("res"):
            continue
        box = idx.get((p["d"], p["tm"]))
        if box is None:
            continue
        pl = box.get(p["n"])
        if pl is None:
            p["res"] = "V"  # no jugó: la apuesta se anula
        else:
            act = sum(pl[i] for i in STAT_IX[p["k"]])
            p["act"] = act
            p["res"] = "P" if act == p["line"] else ("W" if (act > p["line"]) == (p["side"] == "O") else "L")
        n += 1
    return n


def compact(db):
    """Lista compacta de los picks calificados para el dashboard."""
    out = []
    for p in sorted(db["picks"], key=lambda x: (x["d"], -x["edge"])):
        if p.get("res") in ("W", "L", "P"):
            h = p.get("h10") or [0, 0]
            out.append([p["d"], p["n"], p["tm"], p["opp"], p["k"], p["line"], p["side"],
                        round(p["prob"], 3), round(p["edge"], 3), p["res"], p.get("act"), h[0], h[1], p.get("cal", 1),
                        p.get("src", "ref"), p.get("odds", -110), p.get("clv")])
    return out


def fit_shrink(db, prior=300):
    """Corrección de optimismo: probabilidad real = sigmoide(b × logit(prob. del modelo)).
    b se ajusta con los picks ya calificados del modelo calibrado (b < 1 = el modelo exagera).
    Con pocos picks b se acerca a 1 (sin corrección)."""
    rows = [(p.get("raw", p["prob"]), p["res"] == "W") for p in db["picks"]
            if p.get("cal") and p.get("src", "ref") == "ref" and p.get("res") in ("W", "L") and 0 < p.get("raw", p["prob"]) < 1]
    if len(rows) < 100:
        return {"b": 1.0, "n": len(rows)}
    best = None
    for i in range(30, 121):
        b = i / 100
        ll = 0.0
        for p, w in rows:
            q = 1 / (1 + math.exp(-b * math.log(p / (1 - p))))
            ll += math.log(q if w else 1 - q)
        if best is None or ll > best[1]:
            best = (b, ll)
    b = (len(rows) * best[0] + prior) / (len(rows) + prior)
    return {"b": round(b, 3), "fit": best[0], "n": len(rows)}


def start_of(p):
    gs = p.get("gs")
    return datetime.datetime.strptime(gs, "%Y-%m-%dT%H:%MZ") if gs else None


def pending(db):
    return sum(1 for p in db["picks"] if not p.get("res"))
