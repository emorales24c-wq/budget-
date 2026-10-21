"""Backtest en Python de tradingview/smc_strategy.pine (SMC: OB retest + CHoCH interno).

Replica la lógica del script barra a barra:
  - señales al cierre de la vela, entrada a mercado en la apertura siguiente
  - SL/TP como órdenes stop/limit evaluadas dentro de la vela con la misma
    heurística que TradingView (si el high está más cerca de la apertura que el
    low, el recorrido es open -> high -> low -> close; si no, open -> low -> high -> close)
  - TPs recalculados con el precio real de entrada, breakeven/trailing tras TP1

Uso:
  python3 smc_backtest.py datos.csv                 # backtest con parámetros por defecto
  python3 smc_backtest.py datos.csv --optimize      # rejilla en in-sample + validación out-of-sample

El CSV necesita columnas time, open, high, low, close (export de TradingView o yfinance).
"""

import argparse
import itertools
import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

BULL, BEAR = 1, -1


@dataclass
class Params:
    swing_len: int = 50
    int_len: int = 5
    entry_mode: str = 'both'          # 'ob' | 'choch' | 'both'
    direction: str = 'both'           # 'both' | 'long' | 'short'
    use_trend: bool = True
    use_pd: bool = True
    need_confirm: bool = True
    one_trade_per_ob: bool = True
    ob_source: str = 'internal'       # 'internal' | 'swing' | 'both'
    ob_mitig: str = 'close'           # 'close' | 'wick'
    ob_max: int = 5
    ob_max_age: int = 300
    risk_pct: float = 1.0
    qty_dec: int = 0
    sl_mode: str = 'structure'        # 'structure' | 'atr'
    atr_len: int = 14
    sl_buffer: float = 0.2
    sl_atr_mult: float = 1.5
    min_sl_atr: float = 0.3
    max_sl_atr: float = 4.0
    rr1: float = 1.5
    tp1_pct: int = 50
    rr2: float = 3.0
    move_be: bool = True
    use_trail: bool = False
    trail_mult: float = 2.0
    close_on_flip: bool = True
    use_session: bool = False
    session: tuple = (9 * 60 + 30, 16 * 60)   # minutos desde medianoche, hora de NY
    max_trades_day: int = 3


@dataclass
class Instrument:
    tick: float = 0.25
    point_value: float = 2.0          # MNQ: 2 USD por punto
    commission: float = 0.62          # USD por contrato y lado
    slippage_ticks: int = 1           # en órdenes a mercado y stop
    initial_capital: float = 25_000.0


@dataclass
class Pivot:
    level: float = math.nan
    crossed: bool = False
    idx: int = -1


@dataclass
class OB:
    top: float
    btm: float
    idx: int
    born: int
    bias: int
    used: bool = False


@dataclass
class Trade:
    direction: int
    entry_bar: int
    entry_time: object
    entry_price: float
    qty: float
    risk_points: float
    tag: str
    exits: list = field(default_factory=list)   # (bar, time, price, qty, reason)
    commission: float = 0.0

    def pnl(self, inst):
        gross = sum(self.direction * (p - self.entry_price) * q for _, _, p, q, _ in self.exits) * inst.point_value
        return gross - self.commission

    def r_multiple(self, inst):
        risk_cash = self.risk_points * self.qty * inst.point_value
        return self.pnl(inst) / risk_cash if risk_cash > 0 else 0.0


# ---------------------------------------------------------------------------------------------------------------------
# indicadores con la misma definición que Pine
# ---------------------------------------------------------------------------------------------------------------------
def pine_atr(h, l, c, length):
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])])
    atr = np.full(len(c), np.nan)
    if len(c) >= length:
        atr[length - 1] = tr[:length].mean()
        for i in range(length, len(c)):
            atr[i] = (atr[i - 1] * (length - 1) + tr[i]) / length
    return atr


def legs(h, l, size):
    """ta.change(leg(size)) de Pine: -1 = nuevo pivote alto, +1 = nuevo pivote bajo."""
    n = len(h)
    hh = pd.Series(h).rolling(size).max().to_numpy()
    ll = pd.Series(l).rolling(size).min().to_numpy()
    change = np.zeros(n, dtype=int)
    leg = 0
    prev = 0
    for i in range(n):
        if i >= size and not np.isnan(hh[i]):
            if h[i - size] > hh[i]:
                leg = 0
            elif l[i - size] < ll[i]:
                leg = 1
        if i > 0:
            change[i] = leg - prev
        prev = leg
    return change


def round_tick(x, tick):
    return round(x / tick) * tick


# ---------------------------------------------------------------------------------------------------------------------
# motor
# ---------------------------------------------------------------------------------------------------------------------
def run(df, p: Params, inst: Instrument):
    o, h, l, c = (df[k].to_numpy(dtype=float) for k in ('open', 'high', 'low', 'close'))
    times = df['time']
    ny = times.dt.tz_convert('America/New_York')
    day = ny.dt.date.to_numpy()
    minute = (ny.dt.hour * 60 + ny.dt.minute).to_numpy()
    n = len(c)

    atr = pine_atr(h, l, c, p.atr_len)
    atr200 = pine_atr(h, l, c, 200)
    high_vol = ~np.isnan(atr200) & ((h - l) >= 2 * np.nan_to_num(atr200, nan=np.inf))
    ph_arr = np.where(high_vol, l, h)
    pl_arr = np.where(high_vol, h, l)

    swing_ch = legs(h, l, p.swing_len)
    int_ch = legs(h, l, p.int_len)

    swing_high, swing_low, int_high, int_low = Pivot(), Pivot(), Pivot(), Pivot()
    swing_trend = int_trend = 0
    bull_obs, bear_obs = [], []
    trail_top = trail_btm = math.nan

    slip = inst.slippage_ticks * inst.tick
    realized = 0.0
    equity_curve = np.empty(n)
    trades = []

    # estado de la posición
    pos = 0.0
    avg = 0.0
    cur = None
    pending_entry = None      # (dir, qty, tag, risk)
    pending_close = False
    trade_dir = 0
    sig_bar = -1
    sl = tp1 = tp2 = math.nan
    full_qty = 0.0
    tp1_qty = 0.0
    tp1_done = False
    trades_today = 0
    prev_pos = 0.0

    def update_pivots(ch, i, size, ph, pl):
        if ch[i] == -1:
            ph.level, ph.crossed, ph.idx = h[i - size], False, i - size
        if ch[i] == 1:
            pl.level, pl.crossed, pl.idx = l[i - size], False, i - size
        return ch[i] == -1, ch[i] == 1

    def check_break(ph, pl, trend, extra_bull, extra_bear, i):
        if not math.isnan(ph.level) and not ph.crossed and c[i] > ph.level and extra_bull:
            ph.crossed = True
            return (2 if trend == BEAR else 1), BULL
        if not math.isnan(pl.level) and not pl.crossed and c[i] < pl.level and extra_bear:
            pl.crossed = True
            return (-2 if trend == BULL else -1), BEAR
        return 0, trend

    def add_ob(arr, from_idx, bias, i):
        to_idx = i - 1
        if from_idx < 0 or from_idx > to_idx:
            return
        seg = pl_arr[from_idx:to_idx + 1] if bias == BULL else ph_arr[from_idx:to_idx + 1]
        best = from_idx + int(np.argmin(seg) if bias == BULL else np.argmax(seg))
        top = max(ph_arr[best], pl_arr[best])
        btm = min(ph_arr[best], pl_arr[best])
        arr.insert(0, OB(top, btm, best, i, bias))
        if len(arr) > p.ob_max:
            arr.pop()

    def scan_obs(arr, bias, i):
        mit_bull = c[i] if p.ob_mitig == 'close' else l[i]
        mit_bear = c[i] if p.ob_mitig == 'close' else h[i]
        chosen = None
        for k in range(len(arr) - 1, -1, -1):
            ob = arr[k]
            invalid = mit_bull < ob.btm if bias == BULL else mit_bear > ob.top
            if invalid or i - ob.born > p.ob_max_age:
                arr.pop(k)
            elif i > ob.born and not (p.one_trade_per_ob and ob.used):
                touch = l[i] <= ob.top if bias == BULL else h[i] >= ob.btm
                confirm = (not p.need_confirm) or (c[i] > o[i] if bias == BULL else c[i] < o[i])
                if touch and confirm:
                    chosen = ob
        return chosen

    def fill_exit(i, price, qty, reason):
        nonlocal pos, realized
        cur.exits.append((i, times.iloc[i], price, qty, reason))
        cur.commission += inst.commission * qty
        realized += cur.direction * (price - cur.entry_price) * qty * inst.point_value - inst.commission * qty
        pos -= cur.direction * qty

    def process_intrabar(i):
        """Ejecuta stops/limits pendientes dentro de la vela i."""
        nonlocal tp1_qty
        d = cur.direction
        # orden del recorrido intrabarra según TradingView
        path = [o[i], h[i], l[i], c[i]] if abs(h[i] - o[i]) < abs(l[i] - o[i]) else [o[i], l[i], h[i], c[i]]
        targets = []
        if tp1_qty > 0 and not tp1_done:
            targets.append(('TP1', tp1, tp1_qty))
        # si el parcial se redondea a 0 contratos, toda la posición va a TP2
        targets.append(('TP2', tp2, None) if p.tp1_pct < 100 else ('TP', tp1, None))
        filled = set()
        for a, b in zip(path[:-1], path[1:]):
            if pos == 0:
                return
            lo, hi = min(a, b), max(a, b)
            stop_hit = (d == 1 and lo <= sl) or (d == -1 and hi >= sl)
            # en un tramo, el precio pasa primero por lo que está más cerca del punto de partida
            events = []
            if stop_hit:
                gap = (d == 1 and a <= sl) or (d == -1 and a >= sl)
                events.append((0 if gap else abs(sl - a), 'stop', a if gap else sl))
            for name, px, q in targets:
                if name in filled:
                    continue
                if (d == 1 and hi >= px) or (d == -1 and lo <= px):
                    gap = (d == 1 and a >= px) or (d == -1 and a <= px)
                    events.append((0 if gap else abs(px - a), name, a if gap else px, q))
            events.sort(key=lambda e: e[0])
            for ev in events:
                if pos == 0:
                    return
                if ev[1] == 'stop':
                    reason = 'BE/Trail' if tp1_done or abs(pos) < full_qty else 'SL'
                    fill_exit(i, ev[2] - d * slip, abs(pos), reason)
                    return
                name, px, q = ev[1], ev[2], ev[3]
                fill_exit(i, px, abs(pos) if q is None else min(q, abs(pos)), name)
                filled.add(name)
                if name == 'TP1':
                    tp1_qty = 0

    for i in range(n):
        # ---------------- órdenes pendientes que se ejecutan en esta vela ----------------
        if pending_close and pos != 0:
            fill_exit(i, o[i] - cur.direction * slip, abs(pos), 'CHoCH en contra')
            pending_close = False
        if pending_entry is not None:
            d, qty, tag, _ = pending_entry
            price = o[i] + d * slip
            cur = Trade(d, i, times.iloc[i], price, qty, abs(price - sl), tag)
            cur.commission += inst.commission * qty
            realized -= inst.commission * qty
            pos, avg = d * qty, price
            trades.append(cur)
            pending_entry = None
        if pos != 0:
            process_intrabar(i)

        # ---------------- lógica del script al cierre ----------------
        sw_new_h, sw_new_l = update_pivots(swing_ch, i, p.swing_len, swing_high, swing_low)
        update_pivots(int_ch, i, p.int_len, int_high, int_low)

        if sw_new_h:
            trail_top = swing_high.level
        if sw_new_l:
            trail_btm = swing_low.level
        trail_top = max(h[i], h[i] if math.isnan(trail_top) else trail_top)
        trail_btm = min(l[i], l[i] if math.isnan(trail_btm) else trail_btm)
        eq_level = (trail_top + trail_btm) / 2

        int_sig, int_trend = check_break(
            int_high, int_low, int_trend,
            math.isnan(swing_high.level) or int_high.level != swing_high.level,
            math.isnan(swing_low.level) or int_low.level != swing_low.level, i)
        swing_sig, swing_trend = check_break(swing_high, swing_low, swing_trend, True, True, i)

        if p.ob_source != 'swing' and int_sig != 0:
            add_ob(bull_obs if int_sig > 0 else bear_obs, int_high.idx if int_sig > 0 else int_low.idx,
                   BULL if int_sig > 0 else BEAR, i)
        if p.ob_source != 'internal' and swing_sig != 0:
            add_ob(bull_obs if swing_sig > 0 else bear_obs, swing_high.idx if swing_sig > 0 else swing_low.idx,
                   BULL if swing_sig > 0 else BEAR, i)

        bull_hit = scan_obs(bull_obs, BULL, i)
        bear_hit = scan_obs(bear_obs, BEAR, i)

        if i > 0 and day[i] != day[i - 1]:
            trades_today = 0

        just_opened = pos != 0 and prev_pos == 0
        if just_opened and trade_dir != 0:
            r = abs(avg - sl)
            tp1 = round_tick(avg + trade_dir * p.rr1 * r, inst.tick)
            tp2 = round_tick(avg + trade_dir * p.rr2 * r, inst.tick)
            cur.risk_points = r

        if trade_dir != 0 and not tp1_done and pos != 0 and abs(pos) < full_qty * 0.999:
            tp1_done = True
            if p.move_be:
                sl = avg

        if trade_dir != 0 and tp1_done and p.use_trail and pos != 0:
            sl = max(sl, c[i] - p.trail_mult * atr[i]) if trade_dir == 1 else min(sl, c[i] + p.trail_mult * atr[i])

        if p.close_on_flip and ((pos > 0 and swing_sig == -2) or (pos < 0 and swing_sig == 2)):
            pending_close = True

        if trade_dir != 0 and pos == 0 and i > sig_bar:
            trade_dir = 0

        # ---------------- señales ----------------
        in_session = (not p.use_session) or (p.session[0] <= minute[i] < p.session[1])
        day_ok = p.max_trades_day == 0 or trades_today < p.max_trades_day
        can_trade = in_session and day_ok and pos == 0 and trade_dir == 0 and not math.isnan(atr[i]) and i < n - 1
        allow_long = can_trade and p.direction != 'short' and (not p.use_trend or swing_trend == BULL) and (not p.use_pd or c[i] < eq_level)
        allow_short = can_trade and p.direction != 'long' and (not p.use_trend or swing_trend == BEAR) and (not p.use_pd or c[i] > eq_level)
        use_ob = p.entry_mode != 'choch'
        use_choch = p.entry_mode != 'ob'

        cand = None   # (dir, nivel SL, tag, ob)
        if allow_long and use_ob and bull_hit is not None:
            cand = (1, bull_hit.btm, 'OB', bull_hit)
        elif allow_long and use_choch and int_sig == 2 and not math.isnan(int_low.level):
            cand = (1, int_low.level, 'CHoCH', None)
        elif allow_short and use_ob and bear_hit is not None:
            cand = (-1, bear_hit.top, 'OB', bear_hit)
        elif allow_short and use_choch and int_sig == -2 and not math.isnan(int_high.level):
            cand = (-1, int_high.level, 'CHoCH', None)

        if cand is not None:
            d, level, tag, ob = cand
            stop_px = c[i] - d * p.sl_atr_mult * atr[i] if p.sl_mode == 'atr' else level - d * p.sl_buffer * atr[i]
            dist = d * (c[i] - stop_px)
            if dist < p.min_sl_atr * atr[i]:
                dist = p.min_sl_atr * atr[i]
                stop_px = c[i] - d * dist
            qty = 0.0
            if 0 < dist <= p.max_sl_atr * atr[i]:
                equity = inst.initial_capital + realized
                f = 10 ** p.qty_dec
                qty = math.floor(equity * p.risk_pct / 100 / (dist * inst.point_value) * f) / f
            if qty > 0:
                trade_dir, sig_bar = d, i
                sl = round_tick(stop_px, inst.tick)
                tp1 = round_tick(c[i] + d * p.rr1 * dist, inst.tick)
                tp2 = round_tick(c[i] + d * p.rr2 * dist, inst.tick)
                full_qty = qty
                tp1_done = False
                f = 10 ** p.qty_dec
                tp1_qty = math.floor(qty * p.tp1_pct / 100 * f) / f if p.tp1_pct < 100 else 0.0
                trades_today += 1
                if ob is not None:
                    ob.used = True
                pending_entry = (d, qty, tag, dist)

        open_pnl = cur.direction * (c[i] - cur.entry_price) * abs(pos) * inst.point_value if pos != 0 else 0.0
        equity_curve[i] = inst.initial_capital + realized + open_pnl
        prev_pos = pos

    # cerrar lo que quede abierto al final
    if pos != 0:
        fill_exit(n - 1, c[-1], abs(pos), 'Fin de datos')
        equity_curve[-1] = inst.initial_capital + realized

    return trades, pd.Series(equity_curve, index=times)


# ---------------------------------------------------------------------------------------------------------------------
# métricas
# ---------------------------------------------------------------------------------------------------------------------
def stats(trades, equity, inst):
    pnls = np.array([t.pnl(inst) for t in trades])
    rs = np.array([t.r_multiple(inst) for t in trades])
    peak = equity.cummax()
    dd = (equity - peak)
    out = {
        'operaciones': len(trades),
        'beneficio_neto': float(pnls.sum()) if len(pnls) else 0.0,
        'win_rate_%': float((pnls > 0).mean() * 100) if len(pnls) else math.nan,
        'profit_factor': float(pnls[pnls > 0].sum() / -pnls[pnls < 0].sum()) if (pnls < 0).any() else math.inf,
        'R_medio': float(rs.mean()) if len(rs) else math.nan,
        'max_drawdown': float(dd.min()),
        'max_drawdown_%': float((dd / peak).min() * 100),
        'retorno_%': float((equity.iloc[-1] / inst.initial_capital - 1) * 100),
    }
    return out


def trades_frame(trades, inst):
    rows = []
    for t in trades:
        last = t.exits[-1] if t.exits else (None, None, math.nan, 0, '')
        rows.append({
            'entrada': t.entry_time, 'lado': 'Largo' if t.direction == 1 else 'Corto', 'modelo': t.tag,
            'contratos': t.qty, 'precio_entrada': t.entry_price, 'salida': last[1], 'precio_salida': last[2],
            'motivo': ' + '.join(e[4] for e in t.exits), 'pnl': round(t.pnl(inst), 2), 'R': round(t.r_multiple(inst), 2),
        })
    return pd.DataFrame(rows)


def load_csv(path):
    df = pd.read_csv(path)
    df.columns = [col.strip().lower() for col in df.columns]
    tcol = next(col for col in df.columns if col in ('time', 'datetime', 'date', 'timestamp'))
    t = df[tcol]
    if pd.api.types.is_numeric_dtype(t):
        t = pd.to_datetime(t, unit='ms' if t.iloc[0] > 1e11 else 's', utc=True)
    else:
        t = pd.to_datetime(t, utc=True)
    df = pd.DataFrame({'time': t, 'open': df['open'], 'high': df['high'], 'low': df['low'], 'close': df['close']})
    return df.dropna().drop_duplicates('time').sort_values('time').reset_index(drop=True)


GRID = {
    'entry_mode': ['ob', 'choch', 'both'],
    'swing_len': [30, 50, 80],
    'rr2': [2.0, 3.0, 4.0],
    'ob_source': ['internal', 'swing'],
}


def optimize(df, inst, split=0.7):
    cut = int(len(df) * split)
    is_df, oos_df = df.iloc[:cut].reset_index(drop=True), df.iloc[cut:].reset_index(drop=True)
    rows = []
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        p = replace(Params(), **dict(zip(keys, combo)))
        tr, eq = run(is_df, p, inst)
        s = stats(tr, eq, inst)
        rows.append({**dict(zip(keys, combo)), **s})
    res = pd.DataFrame(rows)
    # se exige un mínimo de operaciones para no premiar la suerte
    ranked = res[res['operaciones'] >= 30].sort_values('profit_factor', ascending=False)
    return res, ranked, is_df, oos_df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('csv')
    ap.add_argument('--optimize', action='store_true')
    ap.add_argument('--capital', type=float, default=25_000)
    ap.add_argument('--out', default='resultados')
    args = ap.parse_args()

    inst = Instrument(initial_capital=args.capital)
    df = load_csv(args.csv)
    print(f'{len(df)} velas  {df.time.iloc[0]} -> {df.time.iloc[-1]}')

    trades, eq = run(df, Params(), inst)
    print('\nParámetros por defecto:')
    for k, v in stats(trades, eq, inst).items():
        print(f'  {k:16s} {v:,.2f}')
    trades_frame(trades, inst).to_csv(f'{args.out}_operaciones.csv', index=False)
    eq.to_csv(f'{args.out}_equity.csv')

    if args.optimize:
        res, ranked, is_df, oos_df = optimize(df, inst)
        res.to_csv(f'{args.out}_rejilla.csv', index=False)
        print(f'\nIn-sample: {is_df.time.iloc[0]} -> {is_df.time.iloc[-1]}')
        print(ranked.head(10).to_string(index=False))
        if len(ranked):
            best = ranked.iloc[0]
            p = replace(Params(), **{k: best[k] for k in GRID})
            tr, eq_oos = run(oos_df, p, inst)
            print(f'\nOut-of-sample con el mejor set ({oos_df.time.iloc[0]} -> {oos_df.time.iloc[-1]}):')
            for k, v in stats(tr, eq_oos, inst).items():
                print(f'  {k:16s} {v:,.2f}')


if __name__ == '__main__':
    main()
