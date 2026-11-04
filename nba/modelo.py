"""Modelo NBA calibrado con los partidos ya jugados.

A partir de los box scores guardados (nba/data/boxscores):
  1. Arma el historial partido a partido de cada jugador.
  2. Backtest: proyecta cada partido usando solo lo que se sabía ANTES de ese partido y
     elige con datos el peso de los partidos recientes (minutos y producción por minuto)
     y el tipo de ajuste por rival (ninguno, por equipo o por posición).
  3. Calibra: corrige el sesgo de cada stat, mide el error real (desviación según la
     proyección), elige la distribución (normal o Poisson) y mide back-to-back y el
     aumento de uso cuando falta un jugador de rotación.
  4. Devuelve los minutos y la producción por minuto actuales de cada jugador.
"""
import math
from collections import defaultdict

import dvp

STATS = ["pts", "reb", "ast", "tpm", "stl", "blk"]
COMBOS = {"pra": ["pts", "reb", "ast"], "pr": ["pts", "reb"], "pa": ["pts", "ast"], "ra": ["reb", "ast"]}
LAST_N = 5          # "partidos recientes"
MIN_PRIOR = 3       # partidos previos para entrar al backtest
MIN_MP = 10         # minutos promedio previos para entrar al backtest
OPP_K = 15          # encogimiento del ajuste por rival (partidos de liga)
HOME_ADV = 0.01
GRID = [0, 0.25, 0.5, 0.75, 1]
COUNT_STATS = ["ast", "tpm", "stl", "blk"]


def _date(gid):
    return f"{gid[:4]}-{gid[4:6]}-{gid[6:8]}"


def build_logs(games):
    """{jugador: [ {d, tm, opp, home, role, mp, s:[6]} ]} en orden de fecha, y {equipo: [(fecha, posesiones)]}."""
    logs, team_games = defaultdict(list), defaultdict(list)
    for g in games:
        d, home_tm = _date(g["id"]), g["id"][-3:]
        tms = list(g["teams"])
        for tm in tms:
            opp = tms[1] if tm == tms[0] else tms[0]
            team_games[tm].append((d, g["teams"][tm]["poss"]))
            for name, role, mp, *vals in g["teams"][tm]["players"]:
                logs[name].append({"d": d, "tm": tm, "opp": opp, "home": tm == home_tm, "role": role, "mp": mp, "s": vals})
    return logs, team_games


def _blend(recent, season, w):
    return w * recent + (1 - w) * season


def player_state(entries, a_min, b):
    """Minutos proyectados y producción por minuto (mezcla últimos LAST_N y temporada)."""
    last = entries[-LAST_N:]
    mp_all = sum(e["mp"] for e in entries) / len(entries)
    mp_last = sum(e["mp"] for e in last) / len(last)
    tot_all = sum(e["mp"] for e in entries) or 1
    tot_last = sum(e["mp"] for e in last) or 1
    rates = []
    for k in range(len(STATS)):
        r_all = sum(e["s"][k] for e in entries) / tot_all
        r_last = sum(e["s"][k] for e in last) / tot_last
        rates.append(_blend(r_last, r_all, b))
    return _blend(mp_last, mp_all, a_min), rates


class Snapshot:
    """Lo que se sabía antes de una fecha: lo permitido por cada defensa, posesiones y defensa por posición."""

    def __init__(self, prior_games, positions):
        allowed = defaultdict(lambda: [0.0] * len(STATS))
        n = defaultdict(int)
        poss = defaultdict(list)
        for g in prior_games:
            tms = list(g["teams"])
            for off, de in ((tms[0], tms[1]), (tms[1], tms[0])):
                n[de] += 1
                for _name, _role, _mp, *vals in g["teams"][off]["players"]:
                    allowed[de] = [a + v for a, v in zip(allowed[de], vals)]
                if g["teams"][off]["poss"]:
                    poss[off].append(g["teams"][off]["poss"])
        games = sum(n.values()) or 1
        league = [sum(allowed[t][k] for t in allowed) / games for k in range(len(STATS))]
        self.team_f = {}
        for t in allowed:
            self.team_f[t] = [((allowed[t][k] / n[t]) / league[k] * n[t] + OPP_K) / (n[t] + OPP_K) if league[k] else 1.0
                              for k in range(len(STATS))]
        self.poss = {t: sum(v) / len(v) for t, v in poss.items() if v}
        self.dvp = dvp.compute_dvp(None, positions, prior_games) if prior_games else {}

    def pace(self, tm, opp):
        a, b = self.poss.get(tm), self.poss.get(opp)
        return ((a + b) / 2) / a if a and b else 1.0

    def opp_factor(self, mode, opp, pos, role, k):
        if mode == "team":
            return self.team_f.get(opp, [1.0] * len(STATS))[k]
        if mode == "pos":
            row = self.dvp.get(opp, {}).get(role, {}).get(pos)
            return row[6 + k] if row else 1.0
        return 1.0


def _cases(games, logs, team_games, positions):
    """Partidos-jugador del backtest con todo lo necesario para proyectar sin mirar el futuro."""
    by_date = defaultdict(list)
    for g in games:
        by_date[_date(g["id"])].append(g)
    dates = sorted(by_date)
    team_dates = {t: sorted({d for d, _ in v}) for t, v in team_games.items()}
    cases, prior = [], []
    for d in dates:
        snap = Snapshot(prior, positions)
        for g in by_date[d]:
            tms = list(g["teams"])
            present = {tm: {p[0] for p in g["teams"][tm]["players"] if p[2] > 0} for tm in tms}
            for tm in tms:
                opp = tms[1] if tm == tms[0] else tms[0]
                prev_t = [x for x in team_dates[tm] if x < d]
                b2b = bool(prev_t) and (_dt(d) - _dt(prev_t[-1])).days == 1
                # titulares habituales que faltan (para medir el aumento de uso)
                regulars = {}
                for name, ent in logs.items():
                    pe = [e for e in ent if e["d"] < d and e["tm"] == tm]
                    if len(pe) >= MIN_PRIOR and sum(e["mp"] for e in pe) / len(pe) >= 24:
                        regulars[name] = sum(e["s"][0] for e in pe) / len(pe)
                team_pts = sum(regulars.values()) or 1
                missing_share = sum(v for nm, v in regulars.items() if nm not in present[tm] and _played_recent(logs[nm], tm, prev_t)) / team_pts
                for name, role, mp, *vals in g["teams"][tm]["players"]:
                    if mp <= 0:
                        continue
                    pe = [e for e in logs[name] if e["d"] < d]
                    if len(pe) < MIN_PRIOR or sum(e["mp"] for e in pe) / len(pe) < MIN_MP:
                        continue
                    cases.append({"name": name, "d": d, "tm": tm, "opp": opp, "home": g["id"][-3:] == tm, "role": role,
                                  "pos": positions.get(name, ""), "mp": mp, "act": vals, "prior": pe, "snap": snap,
                                  "b2b": b2b, "miss": missing_share})
        prior.extend(by_date[d])
    return cases


def _dt(d):
    import datetime
    return datetime.date.fromisoformat(d)


def _played_recent(entries, tm, prev_team_dates):
    """El jugador jugó el último partido de su equipo (su ausencia de hoy es nueva)."""
    if not prev_team_dates:
        return False
    last = prev_team_dates[-1]
    return any(e["d"] == last and e["tm"] == tm for e in entries)


def _project(c, a_min, b, mode, role_mode=True):
    mpx, rates = player_state(c["prior"], a_min, b)
    snap = c["snap"]
    h = 1 + HOME_ADV if c["home"] else 1 - HOME_ADV
    pace = snap.pace(c["tm"], c["opp"])
    role = c["prior"][-1]["role"] if role_mode else c["role"]
    out = []
    for k in range(len(STATS)):
        out.append(rates[k] * mpx * pace * h * snap.opp_factor(mode, c["opp"], c["pos"], role, k))
    return mpx, out


def _mae(xs):
    return sum(abs(x) for x in xs) / len(xs) if xs else None


def _norm_cdf(x, m, s):
    return 0.5 * (1 + math.erf((x - m) / (s * math.sqrt(2))))


def _pois_cdf(k, lam):
    s, t = 0.0, math.exp(-lam)
    for i in range(int(k) + 1):
        s += t
        t *= lam / (i + 1)
    return s


def _nb_cdf(k, mu, r):
    if mu <= 0:
        return 1.0
    p = r / (r + mu)
    s, lp = 0.0, r * math.log(p)
    for i in range(int(k) + 1):
        s += math.exp(lp)
        lp += math.log((i + r) / (i + 1)) + math.log(1 - p)
    return min(1.0, s)


def run(games, positions):
    logs, team_games = build_logs(games)
    cases = _cases(games, logs, team_games, positions)
    rep = {"n": len(cases), "games": len(games)}
    if len(cases) < 200:
        return None, logs, team_games
    mean_act = [sum(c["act"][k] for c in cases) / len(cases) or 1 for k in range(len(STATS))]

    # 1) peso de los minutos recientes
    mins = {a: _mae([player_state(c["prior"], a, 0)[0] - c["mp"] for c in cases]) for a in GRID}
    a_min = min(mins, key=mins.get)

    # 2) peso reciente de la producción por minuto y tipo de ajuste por rival
    score, detail = {}, {}
    for b in GRID:
        for mode in ("none", "team", "pos"):
            errs = [[] for _ in STATS]
            for c in cases:
                _, pr = _project(c, a_min, b, mode)
                for k in range(len(STATS)):
                    errs[k].append(pr[k] - c["act"][k])
            maes = [_mae(e) for e in errs]
            score[(b, mode)] = sum(m / mean_act[k] for k, m in enumerate(maes[:4]))
            detail[(b, mode)] = maes
    b, mode = min(score, key=score.get)

    # punto de comparación: el promedio por partido de la temporada, sin ajustes
    base = [[] for _ in STATS]
    for c in cases:
        g = len(c["prior"])
        for k in range(len(STATS)):
            base[k].append(sum(e["s"][k] for e in c["prior"]) / g - c["act"][k])
    rep["mae_base"] = {s: round(_mae(base[k]), 3) for k, s in enumerate(STATS)}
    rep["mae_model"] = {s: round(detail[(b, mode)][k], 3) for k, s in enumerate(STATS)}
    rep["mae_modes"] = {m: {s: round(detail[(b, m)][k], 3) for k, s in enumerate(STATS)} for m in ("none", "team", "pos")}
    rep["mae_min"] = {str(a): round(v, 2) for a, v in mins.items()}

    # 3) sesgo, back-to-back y aumento de uso
    proj = []
    for c in cases:
        _, pr = _project(c, a_min, b, mode)
        proj.append(pr)
    bias = {}
    for k, s in enumerate(STATS):
        sp = sum(p[k] for p in proj)
        bias[s] = round(sum(c["act"][k] for c in cases) / sp, 4) if sp else 1.0
    adj = [[p[k] * bias[s] for k, s in enumerate(STATS)] for p in proj]
    b2b_c = [(c, a) for c, a in zip(cases, adj) if c["b2b"]]
    b2b = 1.0
    if len(b2b_c) >= 80:
        num = sum(sum(c["act"][k] for k in range(3)) for c, _ in b2b_c)
        den = sum(sum(a[k] for k in range(3)) for _, a in b2b_c)
        raw = min(1.05, max(0.9, num / den)) if den else 1.0
        b2b = round(1 + (raw - 1) * len(b2b_c) / (len(b2b_c) + 500), 3)  # muestra chica: se acerca a 1
    rep["b2b_n"] = len(b2b_c)
    # uso: producción por minuto real ÷ proyectada, cuando falta un titular habitual que jugó el partido anterior
    xs = []
    for c, a in zip(cases, adj):
        if c["miss"] > 0 and c["mp"] >= 15:
            mpx, _ = player_state(c["prior"], a_min, b)
            exp_rate = a[0] / mpx if mpx else 0
            if exp_rate > 0:
                xs.append((c["miss"], (c["act"][0] / c["mp"]) / exp_rate - 1))
    usage = 0.4
    if len(xs) >= 50:
        k_u = min(1.0, max(0.0, sum(s * r for s, r in xs) / sum(s * s for s, _ in xs)))
        usage = round((len(xs) * k_u + 100 * 0.3) / (len(xs) + 100), 3)  # se acerca a 0.3 con pocos casos
    rep["usage_n"] = len(xs)

    # 4) error real -> desviación según la proyección, distribución y calibración
    sd, dist, calib, nb, cal = {}, {}, {}, {}, {}
    series = {s: ([a[k] for a in adj], [c["act"][k] for c in cases]) for k, s in enumerate(STATS)}
    for cb, parts in COMBOS.items():
        ks = [STATS.index(p) for p in parts]
        series[cb] = ([sum(a[k] for k in ks) for a in adj], [sum(c["act"][k] for k in ks) for c in cases])
    for s, (mu, act) in series.items():
        res = [abs(y - m) for m, y in zip(mu, act)]
        n = len(mu)
        mx, my = sum(mu) / n, sum(res) / n
        vxx = sum((m - mx) ** 2 for m in mu) or 1
        c1 = sum((m - mx) * (r - my) for m, r in zip(mu, res)) / vxx
        c0 = my - c1 * mx
        k_norm = math.sqrt(math.pi / 2)  # E|e| = sd·√(2/π) en una normal
        sd[s] = [round(max(0.0, c0) * k_norm, 4), round(max(0.0, c1) * k_norm, 4)]
        sdf = lambda m: max(0.5, sd[s][0] + sd[s][1] * m)
        # varias líneas por caso (proyección ± 0, 0.5 y 1 desviación) para medir la calibración en todo el rango
        rows = []
        for m, y in zip(mu, act):
            for z in (-1, -0.5, 0, 0.5, 1):
                L = math.floor(m + z * sdf(m)) + 0.5
                if L > 0:
                    rows.append((m, L, 1 if y > L else 0))
        cands = {"normal": [1 - _norm_cdf(L, m, sdf(m)) for m, L, _ in rows]}
        if s in COUNT_STATS:
            cands["poisson"] = [1 - _pois_cdf(math.floor(L), max(m, 0.01)) for m, L, _ in rows]
            # binomial negativa: varianza = mu + mu²/r, con r ajustado al error real
            num = sum(m * m for m in mu)
            den = sum((y - m) ** 2 - m for m, y in zip(mu, act))
            r_nb = num / den if den > 0 else 50.0
            nb[s] = round(max(0.5, min(50.0, r_nb)), 3)
            cands["negbin"] = [1 - _nb_cdf(math.floor(L), max(m, 0.01), nb[s]) for m, L, _ in rows]
        over = [o for _, _, o in rows]
        brier = {k: sum((p - o) ** 2 for p, o in zip(v, over)) / len(over) for k, v in cands.items()}
        choice = min(brier, key=brier.get)
        probs = cands[choice]
        dist[s] = choice
        # calibración empírica: probabilidad real ≈ α + β·(probabilidad del modelo), ajustada con los casos pasados
        mp_, mo_ = sum(probs) / len(probs), sum(over) / len(over)
        vpp = sum((p - mp_) ** 2 for p in probs) or 1
        beta = sum((p - mp_) * (o - mo_) for p, o in zip(probs, over)) / vpp
        alpha = mo_ - beta * mp_
        cal[s] = [round(alpha, 4), round(beta, 4)]
        probs_cal = [min(0.99, max(0.01, alpha + beta * p)) for p in probs]
        rep.setdefault("brier", {})[s] = {k: round(v, 4) for k, v in brier.items()}
        # tabla de calibración: probabilidad que da el modelo vs lo que pasó (línea = proyección redondeada hacia abajo + 0.5)
        buckets = defaultdict(lambda: [0.0, 0, 0])
        for p, o in zip(probs_cal, over):
            bk = min(9, int(p * 10))
            buckets[bk][0] += p
            buckets[bk][1] += o
            buckets[bk][2] += 1
        calib[s] = [[round(v[0] / v[2], 3), round(v[1] / v[2], 3), v[2]] for bk, v in sorted(buckets.items()) if v[2] >= 15]

    params = {"a_min": a_min, "b": b, "mode": mode, "last_n": LAST_N, "opp_k": OPP_K}
    model = {"params": params, "bias": bias, "sd": sd, "dist": dist, "b2b": b2b, "usage": usage, "calib": calib, "nb": nb, "cal": cal, "report": rep}
    return model, logs, team_games


def current(logs, a_min, b):
    """Minutos proyectados, producción por minuto y rol actual de cada jugador."""
    out = {}
    for name, ent in logs.items():
        ent = [e for e in ent if e["mp"] > 0]
        if not ent:
            continue
        mpx, rates = player_state(ent, a_min, b)
        last = ent[-LAST_N:]
        role = "S" if sum(1 for e in last if e["role"] == "S") * 2 >= len(last) else "B"
        out[name] = {"x": round(mpx, 2), "r": [round(r, 5) for r in rates], "rol": role, "tm": ent[-1]["tm"]}
    return out
