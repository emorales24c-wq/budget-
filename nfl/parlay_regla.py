"""Parlay de props NFL con una regla fija (misma entrada = mismo resultado).

Regla:
1. Props de Kalshi pendientes, de partidos de hoy y mañana, de estos tipos: recepciones, yardas recibidas,
   yardas por tierra, acarreos, yardas de pase.
2. Se descarta al jugador si su lesión está en 'out' o 'duda'.
3. Se cuenta cuántas veces cumplió la línea en la temporada (n partidos) y en sus últimos 5.
   Se pide n >= 8 partidos y que el lado elegido cumpla al menos 60% en la temporada y 60% en los últimos 5.
4. Puntaje = promedio de (tasa de temporada, tasa de últimos 5), con corrección de Laplace.
5. Se toma el mejor de cada partido y no se repite jugador. Empate: orden alfabético por nombre.
6. Parlay = las 3 mejores patas. Precio = producto de precios con comisión de Kalshi.
"""
import json, math, pandas as pd

st = pd.read_csv('nfl/data/raw/stats_player_week_2026.csv', low_memory=False)
st = st[st.season_type == 'REG']
db = json.load(open('nfl/data/picks.json'))
nd = json.load(open('nfl/data/nfl_data.json'))
inj = nd.get('inj', {})
MAP = {'rec': ['receptions'], 'recyd': ['receiving_yards'], 'ryd': ['rushing_yards'],
       'car': ['carries'], 'pyd': ['passing_yards']}
rows = []
for p in db['picks']:
    if p.get('res') or p['d'] < '2026-11-22' or p['d'] > '2026-11-23' or p.get('src') != 'k' or not p.get('c'):
        continue
    if p['k'] not in MAP:
        continue
    st_inj = (inj.get(p['n']) or {}).get('st')
    if st_inj in ('out', 'duda'):
        continue
    pl = st[st.player_display_name == p['n']].sort_values('week')
    if len(pl) < 8:
        continue
    v = sum(pl[c] for c in MAP[p['k']])
    line = p['line']
    hits = [(x > line) if p['side'] == 'O' else (x < line) for x in v]
    last5 = hits[-5:]
    r_s = sum(hits) / len(hits)
    r_5 = sum(last5) / len(last5)
    if r_s < 0.6 or r_5 < 0.6:
        continue
    lap_s = (sum(hits) + 1) / (len(hits) + 2)
    lap_5 = (sum(last5) + 1) / (len(last5) + 2)
    fee = math.ceil(0.07 * p['c'] * (1 - p['c']) * 100) / 100
    rows.append(dict(n=p['n'], tm=p['tm'], opp=p['opp'], k=p['k'], side=p['side'], line=line, c=p['c'], fee=fee,
                     gs=p['gs'], season=f"{sum(hits)}/{len(hits)}", last5=f"{sum(last5)}/{len(last5)}",
                     score=round((lap_s + lap_5) / 2, 3)))
df = pd.DataFrame(rows).sort_values(['score', 'n'], ascending=[False, True])
used_games, used_players, legs = set(), set(), []
for _, r in df.iterrows():
    game = frozenset([r.tm, r.opp])
    if game in used_games or r.n in used_players:
        continue
    legs.append(r); used_games.add(game); used_players.add(r.n)
    if len(legs) == 3:
        break
pd.set_option('display.width', 250)
print(df.head(15).to_string(index=False))
print('\nPARLAY:')
price = 1.0; cost = 1.0
for r in legs:
    print(f"{r.n} ({r.tm}) {r.k} {r.side} {r.line} | temporada {r.season} | últ.5 {r.last5} | precio {r.c} | gs {r.gs}")
    cost *= (r.c + r.fee)
print(f"costo por $1: {cost:.3f}  pago: {1/cost:.1f}x")
