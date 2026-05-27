#%%
import numpy as np
import pandas as pd
import math
import json
import os
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import logging
from typing import Optional, Tuple, Dict, List
from datetime import datetime, timezone

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib import cm
from scipy.interpolate import PchipInterpolator, RegularGridInterpolator
from scipy.stats import gaussian_kde, norm
from scipy.optimize import least_squares

print('Dependencies ready.')
# %%
# Set user-editable parameters

# --- Core asset / horizon ---
UNDERLYING = 'BTC'                  # 'BTC' or 'ETH'
EXPIRY_ISO = None                   # Target option/barrier horizon (YYYY-MM-DD). If None, will use expiry dates from CSV
POLYMARKET_QUOTES_CSV = None        # Path to CSV file from fetch_polymarket_prob.py. If None, auto-detects latest in outputs/
VALUATION_DT = None                  # None = now (UTC) ; or 'YYYY-MM-DD HH:MM' (local)

# --- Monte Carlo config ---
N_PATHS_HIT = 200_000               # Hit probability estimation & sweep envelopes
N_STEPS = None                      # None = ~daily; or set an int
SEED = 42                           # Reproducibility seed used everywhere
MBTCOD = 'bb'                       # 'bb' (bridge), 'gobet', or 'bb_gobet'
USE_MID_SIGMA = True                # Use mid-step sigma in bridge/shift

# --- K sweep ---
SWEEP_POINTS = 301                  # Number of K points across the grid
S_MIN_FACTOR = 0.25                 # Grid min = factor * spot
S_MAX_FACTOR = 3.00                 # Grid max = factor * spot

# --- Gating (Polymarket comparison) ---
ABS_THRESHOLD = 0.05                # Must exceed both CI margin and this absolute threshold

# --- Output locations ---
BASE_DIR = os.getcwd()
SNAPSHOT_DIR = os.path.join(BASE_DIR, 'data_snapshots')
SMILES_DIR = os.path.join(BASE_DIR, 'smiles_term_structure')
OUTPUTS_DIR = os.path.join(BASE_DIR, 'outputs')
FLAGGED_DIR = os.path.join(BASE_DIR, 'flagged')
DERIBIT_OPTION_PRICES_DIR = os.path.join(BASE_DIR, 'deribit_option_prices')
DERIBIT_EXPIRY_HOUR_UTC = int(os.environ.get("DERIBIT_EXPIRY_HOUR_UTC", "8"))

# Output filenames will be generated dynamically based on expiry dates
# Base filenames (will be appended with expiry date)
SWEEP_CSV_BASE = os.path.join(OUTPUTS_DIR, f'{UNDERLYING.lower()}_probability_sweep')
META_JSON_BASE = os.path.join(OUTPUTS_DIR, f'{UNDERLYING.lower()}_probability_sweep_meta')
PLOT_PNG_BASE = os.path.join(OUTPUTS_DIR, f'polymarket_vs_model_{UNDERLYING.lower()}')

# Ensure directories
os.makedirs(SNAPSHOT_DIR, exist_ok=True)
os.makedirs(SMILES_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)
os.makedirs(FLAGGED_DIR, exist_ok=True)
os.makedirs(DERIBIT_OPTION_PRICES_DIR, exist_ok=True)
print('Parameters set.')

#%%
## Functions
def yearfrac_365(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / (365.25 * 24 * 3600.0))


def parse_deribit_expiry_datetime(exp_iso: str, df: Optional[pd.DataFrame] = None) -> datetime:
    """Parse a Deribit expiry, preserving the standard 08:00 UTC expiry time.

    Most repo artifacts historically stored expiries as YYYY-MM-DD strings,
    which silently moved Deribit maturities to midnight when rebuilding the
    surface.  Live fitted smiles now carry ``ExpiryDatetime``; date-only
    fallbacks use Deribit's standard crypto-option expiry time.
    """
    if df is not None and "ExpiryDatetime" in df.columns and len(df):
        raw = str(df["ExpiryDatetime"].dropna().iloc[0]) if df["ExpiryDatetime"].dropna().size else ""
        if raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)

    raw = str(exp_iso)
    if "T" in raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)

    return datetime.strptime(raw[:10], "%Y-%m-%d").replace(
        hour=DERIBIT_EXPIRY_HOUR_UTC,
        minute=0,
        second=0,
        microsecond=0,
        tzinfo=timezone.utc,
    )


def deribit_expiry_dates(option_instruments: List[dict]) -> set:
    """Return calendar dates available in the Deribit option chain."""
    dates = set()
    for inst in option_instruments:
        exp_ms = inst.get("expiration_timestamp")
        if not exp_ms:
            continue
        dates.add(datetime.fromtimestamp(int(exp_ms) / 1000.0, tz=timezone.utc).date())
    return dates


def has_deribit_expiry_on_date(option_instruments: List[dict], target_dt: datetime) -> bool:
    """True when Deribit has an option expiry on the Predict.fun event date."""
    return target_dt.astimezone(timezone.utc).date() in deribit_expiry_dates(option_instruments)


DERIBIT_EXPIRY_WINDOW_DAYS = int(os.environ.get("DERIBIT_EXPIRY_WINDOW_DAYS", "3"))


def has_deribit_expiry_nearby(
    option_instruments: List[dict],
    target_dt: datetime,
    max_days: int = DERIBIT_EXPIRY_WINDOW_DAYS,
) -> bool:
    """True when Deribit has an option expiry within *max_days* of *target_dt*.

    Predict.fun daily markets expire every day at 16:00 UTC, but Deribit
    expiries follow a sparser schedule (e.g. weeklies on Fridays).  Without
    this relaxed check the scanner would produce 0 comparisons every day
    that doesn't happen to have a matching Deribit expiry.
    """
    dates = deribit_expiry_dates(option_instruments)
    target_date = target_dt.astimezone(timezone.utc).date()
    for d in dates:
        if abs((d - target_date).days) <= max_days:
            return True
    return False

def fetch_url_text(url: str, timeout: float = 10.0) -> Optional[str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode('utf-8')
    except Exception:
        return None

def fetch_fred_latest(series_id: str) -> Optional[float]:
    """Fetch latest non-missing annual interest rate value from FRED (percent -> decimal)."""
    url = f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}'
    txt = fetch_url_text(url, timeout=10)
    if not txt:
        return None
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    for ln in reversed(lines[1:]):  # skip header
        parts = ln.split(',')
        if len(parts) < 2:
            continue
        val = parts[1].strip()
        if val not in ('.', '', 'NaN'):
            try:
                return float(val) / 100.0
            except ValueError:
                continue
    return None

def simple_to_cc(y_simple: float, T_years: float, daycount_base: float, quote_base: float) -> float:
    """
    Convert a simple annualized rate (quoted on quote_base) to continuous compounding over T_years.
    df = 1 / (1 + y_simple * T_quote) with T_quote = T_years * (daycount/quote); r_cc = -ln(df) / T_years
    """
    T_quote = T_years * (daycount_base / quote_base)
    df = 1.0 / max(1e-12, 1.0 + y_simple * T_quote)
    return -math.log(df) / max(T_years, 1e-12)

def get_risk_free_cc(T: float) -> Tuple[float, Dict[str, float]]:
    """
    Compute continuous-comp risk-free over T years.
    Preferred: SOFR (ACT/360) -> CC. Fallback: DGS3MO (approx ACT/365) -> CC.
    """
    meta = {'source': '', 'raw_rate': np.nan}
    sofr = fetch_fred_latest('SOFR')
    if sofr is not None and sofr > 0.0:
        r = simple_to_cc(y_simple=sofr, T_years=T, daycount_base=365.25, quote_base=360.0)
        meta.update({'source': 'SOFR (FRED)', 'raw_rate': sofr})
        return float(r), meta
    tbill3m = fetch_fred_latest('DGS3MO')
    if tbill3m is not None and tbill3m > 0.0:
        r = simple_to_cc(y_simple=tbill3m, T_years=T, daycount_base=365.25, quote_base=365.25)
        meta.update({'source': 'DGS3MO (FRED)', 'raw_rate': tbill3m})
        return float(r), meta
    meta.update({'source': 'fallback_zero', 'raw_rate': 0.0})
    return 0.0, meta

def http_get_json(url: str, timeout: float = 10.0) -> Optional[dict]:
    txt = fetch_url_text(url, timeout=timeout)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None

def deribit_api(path: str, params: Dict[str, str]) -> Optional[dict]:
    q = urllib.parse.urlencode(params)
    url = f'{DERIBIT_BASE}{path}?{q}'
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('result') is not None:
                return data['result']   
            return None
    except Exception:
        return None

DERIBIT_BASE = 'https://www.deribit.com/api/v2/'

#############################
## Polymarket API Functions ##
#############################
## NOTE: All Polymarket API fetching functions have been moved to fetch_polymarket_prob.py
## This section is kept for reference only. Use fetch_polymarket_prob.py for API calls.
## To use: from fetch_polymarket_prob import fetch_polymarket_quotes_for_btc

def fetch_spot_binance(underlying: str) -> Optional[float]:
    # Binance pairs follow the pattern '{UNDERLYING}USDT' for all assets.
    symbol = f'{underlying.upper()}USDT'
    url = f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}'
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return float(data['price']) if 'price' in data else None
    except Exception:
        return None

def fetch_spot_fallback_deribit(underlying: str) -> Optional[float]:
    # Deribit index names follow the pattern '{underlying_lower}_usd' for all assets.
    index_name = f'{underlying.lower()}_usd'
    res = deribit_api('public/get_index_price', {'index_name': index_name})
    if res and 'index_price' in res:
        return float(res['index_price'])
    return None

def fetch_spot(underlying: str) -> Optional[float]:
    px = fetch_spot_binance(underlying)
    if px is not None:
        return px
    return fetch_spot_fallback_deribit(underlying)

def _preferred_deribit_price_indexes(currency: str) -> List[str]:
    currency_lower = currency.lower()
    return [f"{currency_lower}_usdc", f"{currency_lower}_usd"]

def _fetch_deribit_instruments(kind: str, currency: str) -> Optional[List[dict]]:
    """
    Fetch Deribit instruments for a base currency.

    Deribit altcoin linear instruments are exposed via USDC-settled products, so
    querying `currency=<altcoin>` can return nothing even when instruments exist.
    We first try the legacy direct currency query, then fall back to a broader
    fetch and filter by base_currency/price_index.
    """
    params = {'currency': currency, 'kind': kind, 'expired': 'false'}
    res = deribit_api('public/get_instruments', params)
    if res:
        return res

    currency_upper = currency.upper()
    price_indexes = set(_preferred_deribit_price_indexes(currency))
    fallback_res = deribit_api('public/get_instruments', {'kind': kind, 'expired': 'false'})
    if not fallback_res:
        return None

    filtered = []
    for inst in fallback_res:
        if inst.get('base_currency', '').upper() != currency_upper:
            continue
        inst_price_index = inst.get('price_index', '').lower()
        if inst_price_index and inst_price_index not in price_indexes:
            continue
        filtered.append(inst)
    return filtered

def deribit_fetch_futures(currency: str) -> Optional[List[dict]]:
    res = _fetch_deribit_instruments('future', currency)
    if not res:
        return None
    return [x for x in res if not x.get('is_perpetual', False)]

def deribit_ticker_mid(instrument_name: str) -> Optional[float]:
    res = deribit_api('public/ticker', {'instrument_name': instrument_name})
    if not res:
        return None
    bid = res.get('best_bid_price') or res.get('bid_price')
    ask = res.get('best_ask_price') or res.get('ask_price')
    mark = res.get('mark_price')
    last = res.get('last_price')
    if bid and ask and bid > 0 and ask > 0:
        return 0.5 * (bid + ask)
    if mark and mark > 0:
        return float(mark)
    if last and last > 0:
        return float(last)
    return None

def compute_carry_from_deribit(spot: float, valuation_dt: datetime, target_T: float, currency: str) -> Tuple[Optional[float], Dict]:
    meta = {'source': 'Deribit', 'used': [], 'interpolation': ''}
    insts = deribit_fetch_futures(currency)
    if not insts:
        return None, meta
    now_ts = valuation_dt.replace(tzinfo=timezone.utc).timestamp()
    pts = []
    for x in insts:
        exp_ms = x.get('expiration_timestamp')
        name = x.get('instrument_name')
        if not exp_ms or not name:
            continue
        exp_ts = exp_ms / 1000.0
        T_i = max(0.0, (exp_ts - now_ts) / (365.25 * 24 * 3600.0))
        if T_i <= 1e-6:
            continue
        mid = deribit_ticker_mid(name)
        if not mid or mid <= 0:
            continue
        c_i = math.log(mid / spot) / T_i
        pts.append((T_i, c_i, name))
    if not pts:
        return None, meta
    pts.sort(key=lambda z: z[0])
    lower = None
    upper = None
    for T_i, c_i, name in pts:
        if T_i < target_T:
            lower = (T_i, c_i, name)
        elif T_i >= target_T and upper is None:
            upper = (T_i, c_i, name)
    if lower and upper and upper[0] > lower[0] + 1e-12:
        Tl, cl, nl = lower
        Th, ch, nh = upper
        w = (target_T - Tl) / (Th - Tl)
        c_T = cl + (ch - cl) * w
        meta['used'] = [{'instrument': nl, 'T': Tl, 'carry': cl}, {'instrument': nh, 'T': Th, 'carry': ch}]
        meta['interpolation'] = 'linear'
        return c_T, meta
    Tn, cn, nn = min(pts, key=lambda z: abs(z[0] - target_T))
    meta['used'] = [{'instrument': nn, 'T': Tn, 'carry': cn}]
    meta['interpolation'] = 'nearest'
    return cn, meta

def get_rates_auto(spot: float, valuation_dt: datetime, expiry_dt: datetime, currency: str) -> Tuple[float, float, Dict]:
    T = yearfrac_365(valuation_dt, expiry_dt)
    r_cc, r_meta = get_risk_free_cc(T)
    c_T, c_meta = compute_carry_from_deribit(spot, valuation_dt, T, currency)
    meta = {
        'r_source': r_meta.get('source', ''),
        'r_raw_rate': r_meta.get('raw_rate', np.nan),
        'c_source': c_meta.get('source', 'Deribit'),
        'c_meta': c_meta,
        'T_years': T,   
    }
    if c_T is None:
        q = 0.0
        meta['note'] = 'Futures basis unavailable; funding set to 0.0'
        return r_cc, q, meta
    q = r_cc - c_T
    return r_cc, float(q), meta    

def deribit_fetch_option_instruments(currency: str) -> Optional[List[dict]]:
    'Fetch all option instruments from Deribit'
    res = _fetch_deribit_instruments('option', currency)
    if not res:
        return None
    return res

def select_expiries_around_target(option_instruments: List[dict], target_dt: datetime, max_expiries: int = 6) -> List[datetime]:
    # Collect unique expiry datetimes
    exps_ms = sorted(set(int(x['expiration_timestamp']) for x in option_instruments if x.get('expiration_timestamp')))
    exps_dt = [datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) for ms in exps_ms]
    if not exps_dt:
        return []
    # Rank by distance to target, ensure at least one below and above if possible
    exps_sorted = sorted(exps_dt, key=lambda d: abs((d - target_dt).total_seconds()))
    chosen = []
    for d in exps_sorted:
        if d not in chosen:
            chosen.append(d)
        if len(chosen) >= max_expiries:
            break
    # Sort ascending
    chosen = sorted(chosen)
    return chosen

def build_smiles_for_expiries(currency: str, chosen_expiries: List[datetime]) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    '''
    For each expiry, fetch all option tickers and extract 'mark_iv' by strike.
    Returns a tuple:
    - smiles: dict: expiry_iso -> DataFrame with columns ['Strike', 'Implied Volatility', 'Expiry']
    - option_prices: dict: expiry_iso -> DataFrame with columns ['Strike', 'Expiry', 'Mark_Price', 'Bid_Price', 'Ask_Price', 'Instrument_Name']
    'Implied Volatility' in percent for easier CSV inspection.
    '''
    insts = deribit_fetch_option_instruments(currency)
    if not insts:
        return {}, {}
    # Bucket instruments by expiry
    buckets: Dict[int, List[dict]] = {}
    for x in insts:
        exp_ms = x.get('expiration_timestamp')
        strike = x.get('strike')
        if not exp_ms or strike is None:
            continue
        buckets.setdefault(exp_ms, []).append(x)

    smiles: Dict[str, pd.DataFrame] = {}
    option_prices: Dict[str, pd.DataFrame] = {}
    for exp_dt in chosen_expiries:
        exp_ms = int(exp_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        group = buckets.get(exp_ms, [])
        rows = []
        price_rows = []
        for inst in group:
            name = inst.get('instrument_name')
            strike = inst.get('strike')
            if not name or strike is None:
                continue
            tick = deribit_api('public/ticker', {'instrument_name': name})
            if not tick:
                continue
            mark_iv = tick.get('mark_iv')
            if mark_iv is None or mark_iv <= 0:
                continue
            iv_val = float(mark_iv)
            iv_pct = iv_val * 100.0 if iv_val <= 1.5 else iv_val
            rows.append({'Strike': float(strike), 'Implied Volatility': iv_pct})
            
            # Also store option prices
            mark_price = tick.get('mark_price') or tick.get('last_price')
            bid_price = tick.get('best_bid_price') or tick.get('bid_price')
            ask_price = tick.get('best_ask_price') or tick.get('ask_price')
            price_rows.append({
                'Strike': float(strike),
                'Expiry': exp_dt.strftime('%Y-%m-%d'),
                'Mark_Price': float(mark_price) if mark_price else None,
                'Bid_Price': float(bid_price) if bid_price else None,
                'Ask_Price': float(ask_price) if ask_price else None,
                'Instrument_Name': name
            })
        if not rows:
            continue
        df = (
            pd.DataFrame(rows)
            .dropna()
            .groupby("Strike", as_index=False)["Implied Volatility"]
            .median()
            .sort_values("Strike")
        )
        df['Expiry'] = exp_dt.strftime('%Y-%m-%d')
        df['ExpiryDatetime'] = exp_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        smiles[df['Expiry'].iloc[0]] = df
        
        # Store option prices DataFrame
        if price_rows:
            price_df = pd.DataFrame(price_rows).sort_values('Strike')
            option_prices[exp_dt.strftime('%Y-%m-%d')] = price_df
    
    return smiles, option_prices


def svi_total_variance_raw(k: np.ndarray, a: float, b: float, rho: float, m: float, sigma: float) -> np.ndarray:
    x = k - m
    return a + b * (rho * x + np.sqrt(x * x + sigma * sigma))

def _svi_bounds():
# a>=0, b>=0, rho in (-0.999,0.999), m free, sigma>0
    lb = np.array([0.0, 0.0, -0.999, -np.inf, 1e-6])
    ub = np.array([np.inf, np.inf, 0.999, np.inf, np.inf])
    return lb, ub

def fit_svi_raw(k: np.ndarray, w: np.ndarray) -> Tuple[np.ndarray,Dict[str, float]]:
## Robust least-squares fit to w(k). Returns params [a,b,rho,m,sigma] and diagnostics.

    k = np.asarray(k, dtype=float)
    w = np.asarray(w, dtype=float)
    # Trim obvious outliers/nans
    mask = np.isfinite(k) & np.isfinite(w) & (w > 1e-8) & (w < 50.0) #cap huge outliers
    k, w = k[mask], w[mask]
    if k.size < 5:
        raise ValueError("Not enough data points to fit SVI")
    lb, ub = _svi_bounds()
    # Initial guesses
    k_med = float(np.median(k))
    w_min = float(np.percentile(w, 10))
    skew = np.corrcoef(k, w)[0, 1] if np.std(k) > 1e-9 and np.std(w) > 1e-9 else 0.0
    guesses = []
    for b0 in [0.1, 0.5, 1.0, 2.0]:
        for rho0 in [-0.7, -0.3, 0.0, 0.3, 0.7]:
            for sig0 in [0.05, 0.15, 0.30]:
                m0 = k_med
                a0 = max(1e-6, w_min * 0.8)
                p0 = np.array([a0, b0, rho0, m0, sig0])
                guesses.append(p0)
    # Bias the sign of rho with skew if available
    if skew < -0.05:
        guesses.append(np.array([w_min, 0.8, -0.6, k_med, 0.2]))
    elif skew > 0.05:
        guesses.append(np.array([w_min, 0.8, 0.6, k_med, 0.2]))
    else:
        guesses.append(np.array([w_min, 0.8, 0.0, k_med, 0.2]))
    best = None
    best_cost = np.inf

    def residuals(p):
        a, b, rho, m, sigma = p
        ww = svi_total_variance_raw(k, a, b, rho, m, sigma)
        return ww - w

    for p0 in guesses:
        try:
            res = least_squares(
                residuals,
                x0=p0,
                bounds=(lb, ub),
                loss="soft_l1",
                f_scale=np.median(np.abs(w - np.median(w))) + 1e-6,
                max_nfev=5000,
            )
            cost = np.mean((residuals(res.x)) ** 2)
            if np.isfinite(cost) and cost < best_cost:
                best = res
                best_cost = cost
        except Exception:
            continue

    if best is None:
        raise RuntimeError("SVI fit failed for this expiry")
    
    p = best.x
    rmse = float(np.sqrt(best_cost))
    return p, {"rmse_w": rmse, "n": int(k.size)}




def fit_svi_smile_for_expiry(
    spot: float,
    r: float,
    q: float,
    valuation_dt: datetime,
    expiry_dt: datetime,
    df_raw: pd.DataFrame,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    """
    Fit SVI to raw mark IVs for a single expiry and return a smooth smile DataFrame and diagnostics.
    df_raw columns: Strike, Implied Volatility (percent)
    """
    T = yearfrac_365(valuation_dt, expiry_dt)
    if T <= 0:
        raise ValueError("Expiry already passed")
    F = spot * np.exp((r - q) * T)

    sdf = df_raw[["Strike", "Implied Volatility"]].dropna().copy()
    sdf["Strike"] = sdf["Strike"].astype(float)
    # Convert percent -> decimal
    iv_dec = np.clip(pd.to_numeric(sdf["Implied Volatility"], errors="coerce") / 100.0, 1e-4, 5.0)
    k = np.log(np.clip(sdf["Strike"].values, 1e-9, 1e12) / F)
    w = np.clip(iv_dec.values ** 2 * T, 1e-8, 50.0)

    # Fit SVI
    params, diag = fit_svi_raw(k, w)
    a, b, rho, m, sigma = params

    # Build smooth strike grid across observed range with modest extension
    k_lo, k_hi = float(np.min(k)), float(np.max(k))
    k_grid = np.linspace(k_lo - 0.10 * (k_hi - k_lo + 1e-6), k_hi + 0.10 * (k_hi - k_lo + 1e-6), 200)
    w_fit = np.clip(svi_total_variance_raw(k_grid, a, b, rho, m, sigma), 1e-8, 50.0)
    iv_fit = np.sqrt(w_fit / T)
    K_grid = F * np.exp(k_grid)

    out = pd.DataFrame({
        "Strike": K_grid.astype(float),
        "Implied Volatility": (iv_fit * 100.0).astype(float),
        "Expiry": expiry_dt.strftime("%Y-%m-%d"),
        "ExpiryDatetime": expiry_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }).dropna().sort_values("Strike")

    diag_out = {"a": float(a), "b": float(b), "rho": float(rho), "m": float(m), "sigma": float(sigma)}
    diag_out.update(diag)
    return out, diag_out


def fit_svi_smiles_for_expiries(
    spot: float,
    r: float,
    q: float,
    valuation_dt: datetime,
    expiries: List[datetime],
    currency: str = None,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, Dict[str, float]], Dict[str, pd.DataFrame]]:
    """
    For each expiry, build a raw Deribit smile and fit SVI, returning fitted smiles, params, and option prices.
    Returns:
        fitted: Dict of fitted smiles
        params: Dict of SVI parameters
        option_prices: Dict of option prices DataFrames
    """
    if currency is None:
        currency = UNDERLYING
    fitted: Dict[str, pd.DataFrame] = {}
    params: Dict[str, Dict[str, float]] = {}
    raw, option_prices = build_smiles_for_expiries(currency, expiries)
    for exp_dt in expiries:
        key = exp_dt.strftime("%Y-%m-%d")
        df_raw = raw.get(key)
        if df_raw is None or df_raw.empty:
            continue
        try:
            out_df, diag = fit_svi_smile_for_expiry(spot, r, q, valuation_dt, exp_dt, df_raw)
            fitted[key] = out_df
            params[key] = diag
        except Exception as e:
            # Skip if fitting fails for this expiry
            continue
    return fitted, params, option_prices



    # Overlay the local SVI smile vs Deribit raw smile, then compare for nearest common expiry

def _nearest_deribit_exp_for_date(opt_insts: List[dict], target_iso: str) -> datetime:
    """Pick Deribit expiry whose date matches target_iso (YYYY-MM-DD); if none, nearest by days."""
    # Build list of datetimes
    exps_ms = sorted(set(int(x["expiration_timestamp"]) for x in opt_insts if x.get("expiration_timestamp")))
    exps_dt = [datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc) for ms in exps_ms]
    # Try exact date match first
    for d in exps_dt:
        if d.strftime("%Y-%m-%d") == target_iso:
            return d
    # Nearest by absolute days
    tgt = datetime.strptime(target_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return min(exps_dt, key=lambda d: abs((d - tgt).days))


#############################
## Dupire Local Volatility ##
#############################

def bs_call_from_forward(F, K, df, T, sigma):
    if T <= 0:
        return df * max(F - K, 0.0)
    sqrtT = np.sqrt(T)
    vol_sqrtT = sigma * sqrtT
    if vol_sqrtT < 1e-12:
        return df * max(F - K, 0.0)
    d1 = (np.log(F / K) + 0.5 * sigma * sigma * T) / vol_sqrtT
    d2 = d1 - vol_sqrtT
    return df * (F * norm.cdf(d1) - K * norm.cdf(d2))


class ImpliedVolSurface:
    def __init__(self, spot, r, q, Ts, interps):
        self.spot = float(spot)
        self.r = float(r)
        self.q = float(q)
        self.Ts = np.array(Ts, dtype=float)
        self.interps = interps
        self.vol_bounds = (1e-4, 5.0)

    @classmethod
    def from_smile_dict(cls, smiles: Dict[str, pd.DataFrame], spot: float, r: float, q: float, valuation_dt: datetime):
        entries = []
        for exp_iso, df in smiles.items():
            try:
                expiry_dt = parse_deribit_expiry_datetime(exp_iso, df)
            except Exception:
                continue
            T = yearfrac_365(valuation_dt, expiry_dt)
            if T <= 0:
                continue
            sdf = df[["Strike", "Implied Volatility"]].dropna().copy()
            sdf.columns = ["Strike", "IVpct"]
            sdf = sdf.drop_duplicates("Strike").sort_values("Strike")
            sdf["IV"] = sdf["IVpct"].astype(float) / 100.0
            sdf["Strike"] = sdf["Strike"].astype(float)
            entries.append((T, sdf))
        if not entries:
            raise ValueError("No valid future expiries found.")
        entries.sort(key=lambda x: x[0])
        Ts = []
        interps = []
        for T, sdf in entries:
            F = spot * np.exp((r - q) * T)
            k = np.log(sdf["Strike"].values / F)
            vols = sdf["IV"].values
            interp = PchipInterpolator(k, vols, extrapolate=True)
            Ts.append(T)
            interps.append(interp)
        return cls(spot=spot, r=r, q=q, Ts=np.array(Ts), interps=interps)

    def forward(self, T):
        return self.spot * np.exp((self.r - self.q) * float(T))

    def _sigma_k_T(self, k, T):
        T = float(T)
        Ts = self.Ts
        if T <= Ts[0]:
            sigma = float(np.clip(self.interps[0](k), *self.vol_bounds))
            return sigma
        if T >= Ts[-1]:
            sigma = float(np.clip(self.interps[-1](k), *self.vol_bounds))
            return sigma
        idx = np.searchsorted(Ts, T)
        Tl, Th = Ts[idx - 1], Ts[idx]
        wl = np.clip(self.interps[idx - 1](k), *self.vol_bounds) ** 2 * Tl
        wh = np.clip(self.interps[idx](k), *self.vol_bounds) ** 2 * Th
        w = wl + (wh - wl) * ((T - Tl) / (Th - Tl))
        sigma = np.sqrt(max(w, 1e-12) / T)
        return float(np.clip(sigma, *self.vol_bounds))

    def sigma(self, K, T):
        F = self.forward(T)
        k = np.log(float(K) / F)
        return self._sigma_k_T(k, T)

    def call_price(self, K, T):
        F = self.forward(T)
        df = np.exp(-self.r * float(T))
        sigma = self.sigma(K, T)
        return bs_call_from_forward(F, K, df, T, sigma)

    def tail_probability_from_smile(self, K, T, rel_bump=1e-3):
        T = float(T)
        r = self.r
        eps = max(1e-8, rel_bump * K)
        Kp, Km = K + eps, max(1e-8, K - eps)
        Cp = self.call_price(Kp, T)
        Cm = self.call_price(Km, T)
        dC_dK = (Cp - Cm) / (Kp - Km)
        P = -np.exp(r * T) * dC_dK
        return float(np.clip(P, 0.0, 1.0))

class LocalVolSurface:
    def __init__(
        self,
        iv_surface: ImpliedVolSurface,
        T_end: float,
        S_grid: np.ndarray,
        t_grid: np.ndarray,
        dK_rel: float = 5e-3,
        dT_abs: float = 5e-4,
        min_var: float = 1e-8,
        max_var: float = 25.0,
    ):
        self.ivs = iv_surface
        self.T_end = float(T_end)
        self.S_grid = np.asarray(S_grid, dtype=float)
        self.logS_grid = np.log(self.S_grid)
        self.t_grid = np.asarray(t_grid, dtype=float)
        self.dK_rel = dK_rel
        self.dT_abs = dT_abs
        self.min_var = min_var
        self.max_var = max_var
        self._sigma_grid = self._build_sigma_grid()
        self._interp = RegularGridInterpolator(
            (self.t_grid, self.logS_grid), self._sigma_grid, bounds_error=False, fill_value=None
        )

    # ---- Price derivatives ----
    def _call(self, K, T):
        return self.ivs.call_price(K, T)

    def _dC_dK(self, K, T):
        h = max(1e-8, self.dK_rel * K)
        Cp = self._call(K + h, T)
        Cm = self._call(max(1e-8, K - h), T)
        return (Cp - Cm) / (2.0 * h)

    def _d2C_dK2(self, K, T):
        h = max(1e-8, self.dK_rel * K)
        Cp = self._call(K + h, T)
        C0 = self._call(K, T)
        Cm = self._call(max(1e-8, K - h), T)
        return (Cp - 2.0 * C0 + Cm) / (h * h)

    def _dC_dT(self, K, T):
        h = max(self.dT_abs, 1e-6)
        T_lo = max(self.t_grid[0], T - h)
        T_hi = min(self.t_grid[-1], T + h)
        if T_hi == T_lo:
            T_hi = min(self.t_grid[-1], T + max(1e-6, 0.5 * h))
            T_lo = max(self.t_grid[0], T - max(1e-6, 0.5 * h))
        Cp = self._call(K, T_hi)
        Cm = self._call(K, T_lo)
        return (Cp - Cm) / (T_hi - T_lo)

    def _sigma_loc_point(self, T, K):
        r, q = self.ivs.r, self.ivs.q
        C = self._call(K, T)
        dC_dK = self._dC_dK(K, T)
        d2C_dK2 = self._d2C_dK2(K, T)
        dC_dT = self._dC_dT(K, T)
        num = dC_dT + (r - q) * K * dC_dK + q * C
        den = 0.5 * K * K * max(d2C_dK2, 0.0)
        if den <= 1e-16:
            sigma_fallback = self.ivs.sigma(K, T)
            return float(np.clip(sigma_fallback, 1e-4, 5.0))
        var = np.clip(num / den, self.min_var, self.max_var)
        return float(np.sqrt(var))

    def _build_sigma_grid(self):
        tmin = max(1e-6, min(self.ivs.Ts[0] + self.dT_abs, self.T_end))
        tmax = min(self.T_end, self.ivs.Ts[-1] - self.dT_abs)
        if tmax <= tmin:
            raise ValueError(
                "Time grid cannot be built: ensure expiries bracket the target maturity."
            )
        tg = np.clip(self.t_grid, tmin, tmax)
        sig_grid = np.zeros((len(tg), len(self.S_grid)), dtype=float)
        for i, T in enumerate(tg):
            for j, S in enumerate(self.S_grid):
                sig_grid[i, j] = self._sigma_loc_point(T, S)
        return sig_grid

    def sigma(self, t, s):
        t = float(np.clip(t, self.t_grid[0], self.t_grid[-1]))
        logS = float(np.log(np.clip(s, self.S_grid[0], self.S_grid[-1])))
        return float(np.clip(self._interp([[t, logS]])[0], 1e-4, 5.0))

    def sigma_vec(self, t, s_vec):
        t = float(np.clip(t, self.t_grid[0], self.t_grid[-1]))
        s = np.clip(s_vec, self.S_grid[0], self.S_grid[-1])
        pts = np.column_stack([np.full_like(s, t, dtype=float), np.log(s)])
        vals = self._interp(pts)
        return np.clip(vals, 1e-4, 5.0)


############################
## Monte Carlo Simulation ##
############################
def wilson_interval(prob: float, n: int, confidence: float = 0.95):
    z = norm.ppf((1 + confidence) / 2)
    n = float(n)
    denom = 1 + z * z / n
    center = (prob + z * z / (2 * n)) / denom
    margin = z * np.sqrt(prob * (1 - prob) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def wilson_interval_array(prob: np.ndarray, n: int, confidence: float = 0.95):
    z = norm.ppf((1 + confidence) / 2)
    n = float(n)
    denom = 1.0 + (z * z) / n
    center = (prob + (z * z) / (2.0 * n)) / denom
    margin = z * np.sqrt(prob * (1.0 - prob) / n + (z * z) / (4.0 * n * n)) / denom
    lo = np.clip(center - margin, 0.0, 1.0)
    hi = np.clip(center + margin, 0.0, 1.0)
    return lo, hi

class LocalVolMC:
    def __init__(self, iv_surface: ImpliedVolSurface, lv_surface: LocalVolSurface, seed: int | None = None):
        self.ivs = iv_surface
        self.lvs = lv_surface
        self.r = iv_surface.r
        self.q = iv_surface.q
        if seed is not None:
            np.random.seed(seed)

    def simulate_touch_probability(
        self,
        S0,
        K_barrier,
        T,
        n_paths=200_000,
        n_steps=None,
        mBTCod="bb",
        use_mid_sigma=True,
        seed=None,
    ):
        if seed is not None:
            np.random.seed(seed)
        S0 = float(S0)
        T = float(T)
        if n_steps is None:
            n_steps = max(5, int(np.ceil(T * 365)))  # ~daily
        dt = T / n_steps
        sqrt_dt = np.sqrt(dt)

        up_barrier = K_barrier >= S0
        H = np.log(K_barrier)
        gobet_c = 0.5826

        X = np.full(n_paths, np.log(S0), dtype=float)
        hit = np.zeros(n_paths, dtype=bool)

        for j in range(n_steps):
            t = j * dt
            S = np.exp(X)
            sigma_t = self.lvs.sigma_vec(t, S)
            mu_log = (self.r - self.q) - 0.5 * sigma_t * sigma_t
            Z = np.random.standard_normal(n_paths)
            X_next = X + mu_log * dt + sigma_t * sqrt_dt * Z
            S_next = np.exp(X_next)

            if mBTCod in ("gobet", "bb_gobet"):
                sigma_for_shift = (
                    sigma_t if not use_mid_sigma else self.lvs.sigma_vec(t + 0.5 * dt, np.sqrt(S * S_next))
                )
                shift = gobet_c * sigma_for_shift * sqrt_dt
                if up_barrier:
                    K_shift = K_barrier * np.exp(-shift)
                    disc_cross = (S >= K_shift) | (S_next >= K_shift)
                else:
                    K_shift = K_barrier * np.exp(+shift)
                    disc_cross = (S <= K_shift) | (S_next <= K_shift)
                hit |= disc_cross
            else:
                if up_barrier:
                    hit |= (S_next >= K_barrier) | (S >= K_barrier)
                else:
                    hit |= (S_next <= K_barrier) | (S <= K_barrier)

            if mBTCod in ("bb", "bb_gobet"):
                not_hit = ~hit
                if np.any(not_hit):
                    X0 = X[not_hit]
                    X1 = X_next[not_hit]
                    if up_barrier:
                        mask = (X0 < H) & (X1 < H)
                    else:
                        mask = (X0 > H) & (X1 > H)
                    idxs = np.where(not_hit)[0][mask]
                    if idxs.size > 0:
                        X0m = X[idxs]
                        X1m = X_next[idxs]
                        sigma_mid = (
                            self.lvs.sigma_vec(t + 0.5 * dt, np.sqrt(np.exp(X0m) * np.exp(X1m)))
                            if use_mid_sigma
                            else self.lvs.sigma_vec(t, np.exp(X0m))
                        )
                        var = np.maximum(sigma_mid * sigma_mid * dt, 1e-12)
                        if up_barrier:
                            a = H - X0m
                            b = H - X1m
                        else:
                            a = X0m - H
                            b = X1m - H
                        p_cross = np.exp(-2.0 * a * b / var)
                        u = np.random.random(size=idxs.size)
                        crossed = u < np.clip(p_cross, 0.0, 1.0)
                        hit[idxs[crossed]] = True

            X = X_next
            if hit.all():
                break

        prob = float(hit.mean())
        lo, hi = wilson_interval(prob, n_paths, confidence=0.95)
        return prob, (lo, hi)

    def simulate_paths(self, S0, T, n_paths=50_000, n_steps=None, seed=None):
        if seed is not None:
            np.random.seed(seed)
        S0 = float(S0)
        T = float(T)
        if n_steps is None:
            n_steps = max(5, int(np.ceil(T * 365)))
        dt = T / n_steps
        sqrt_dt = np.sqrt(dt)

        X = np.full(n_paths, np.log(S0))
        paths = np.empty((n_paths, n_steps + 1), dtype=float)
        paths[:, 0] = S0
        for j in range(1, n_steps + 1):
            t = (j - 1) * dt
            S = np.exp(X)
            sigma_t = self.lvs.sigma_vec(t, S)
            mu_log = (self.r - self.q) - 0.5 * sigma_t * sigma_t
            Z = np.random.standard_normal(n_paths)
            X = X + mu_log * dt + sigma_t * sqrt_dt * Z
            paths[:, j] = np.exp(X)
        return paths

    def simulate_touch_envelopes(
        self,
        S0,
        T,
        n_paths=200_000,
        n_steps=None,
        mBTCod="bb",
        use_mid_sigma=True,
        seed=None,
    ):
        if seed is not None:
            np.random.seed(seed)
        S0 = float(S0)
        T = float(T)
        if n_steps is None:
            n_steps = max(5, int(np.ceil(T * 365)))
        dt = T / n_steps
        sqrt_dt = np.sqrt(dt)
        gobet_c = 0.5826

        X = np.full(n_paths, np.log(S0), dtype=float)
        M_up = np.full(n_paths, S0, dtype=float)
        M_down = np.full(n_paths, S0, dtype=float)

        for j in range(n_steps):
            t = j * dt
            S = np.exp(X)
            sigma_t = self.lvs.sigma_vec(t, S)
            mu_log = (self.r - self.q) - 0.5 * sigma_t * sigma_t
            Z = np.random.standard_normal(n_paths)
            X_next = X + mu_log * dt + sigma_t * sqrt_dt * Z
            S_next = np.exp(X_next)
            sigma_mid = (
                self.lvs.sigma_vec(t + 0.5 * dt, np.sqrt(S * S_next)) if use_mid_sigma else sigma_t
            )

            if mBTCod in ("gobet", "bb_gobet"):
                shift = gobet_c * sigma_mid * sqrt_dt
                M_up = np.maximum(M_up, np.maximum(S, S_next) * np.exp(shift))
                M_down = np.minimum(M_down, np.minimum(S, S_next) * np.exp(-shift))
            else:
                M_up = np.maximum(M_up, np.maximum(S, S_next))
                M_down = np.minimum(M_down, np.minimum(S, S_next))

            if mBTCod in ("bb", "bb_gobet"):
                var = np.maximum(sigma_mid * sigma_mid * dt, 1e-12)
                U = np.clip(np.random.random(n_paths), 1e-12, 1.0 - 1.0e-12)
                c = -0.5 * var * np.log(U)
                delta = np.sqrt((X_next - X) ** 2 + 4.0 * c)
                H_plus = 0.5 * (X + X_next + delta)
                H_minus = 0.5 * (X + X_next - delta)
                M_up = np.maximum(M_up, np.exp(H_plus))
                M_down = np.minimum(M_down, np.exp(H_minus))

            X = X_next

        ST = np.exp(X)
        return M_up, M_down, ST


###################
## Helper Functions ##
###################

def parse_question_type_from_slug(slug: str, strike_K: float, question: str = "") -> Tuple[str, Optional[float]]:
    """
    Parse question type from Polymarket groupItemTitle, question text, and slug.

    Priority order (most reliable first):
      1. groupItemTitle symbols embedded in question as "[<78,000]" or "[>96,000]"
         or "[78,000-80,000]" — these are structured by Polymarket.
      2. Keywords in the question text ("below", "above", "less than", etc.)
      3. Keywords in the slug ("less-than", "greater-than", etc.)
      4. Default: 'above'

    Returns (question_type, upper_K).
    """
    text = f"{question} {slug}".lower()

    # --- Priority 1: structured groupItemTitle symbols ("[<N]", "[>N]", "[N-N]") ---
    bracket_match = re.search(r'\[([<>]?)(\d[\d,]*(?:\.\d+)?)\s*(?:-\s*(\d[\d,]*(?:\.\d+)?))?\]', text)
    if bracket_match:
        prefix = bracket_match.group(1)   # '<', '>', or ''
        num1 = bracket_match.group(2)
        num2 = bracket_match.group(3)      # second number if range like "78,000-80,000"
        if num2:
            upper = float(num2.replace(',', ''))
            return 'between', upper
        if prefix == '<':
            return 'below', None
        if prefix == '>':
            return 'above', None
        # plain number in brackets (e.g. "[80,000]") — ambiguous, fall through

    # --- Priority 2: detect bare < or > followed by a number anywhere in text ---
    if re.search(r'<\s*\$?\d', text):
        return 'below', None
    if re.search(r'>\s*\$?\d', text):
        return 'above', None

    # --- Priority 3: keyword matching ---
    BETWEEN_WORDS = ['between']
    BELOW_WORDS = ['below', 'under', 'less than', 'less-than', 'lower than', 'dip to', 'dip below']
    ABOVE_WORDS = ['above', 'over', 'more than', 'greater than', 'greater-than', 'higher than', 'reach']

    if any(w in text for w in BETWEEN_WORDS):
        numbers = re.findall(r'[\$]?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)', text)
        prices = sorted(set(int(n.replace(',', '')) for n in numbers if int(n.replace(',', '')) >= 1000))
        if len(prices) >= 2:
            for i in range(len(prices) - 1):
                if abs(prices[i] - strike_K) < 1:
                    return 'between', float(prices[i + 1])
            return 'between', float(prices[1])
        return 'between', float(strike_K + 2000)

    if any(w in text for w in BELOW_WORDS):
        return 'below', None

    if any(w in text for w in ABOVE_WORDS):
        return 'above', None

    return 'above', None


def load_polymarket_quotes_from_csv(csv_path: Optional[str] = None) -> Dict[str, List[Tuple[float, float, str, Optional[float], str]]]:
    """
    Load Polymarket quotes from CSV file and group by expiry_date.
    
    Args:
        csv_path: Path to CSV file. If None, auto-detects latest polymarket_quotes_*.csv in outputs/
        
    Returns:
        Dictionary mapping expiry_date (YYYY-MM-DD) to list of
        (K, probability, question_type, upper_K, slug) tuples.
        question_type is 'above', 'below', or 'between'.
        upper_K is the upper bound for 'between' questions, None otherwise.
    """
    if csv_path is None:
        csv_files = [f for f in os.listdir(OUTPUTS_DIR) if f.startswith('polymarket_quotes_') and f.endswith('.csv')]
        if not csv_files:
            raise FileNotFoundError(f"No polymarket_quotes_*.csv files found in {OUTPUTS_DIR}")
        csv_files.sort(key=lambda f: os.path.getmtime(os.path.join(OUTPUTS_DIR, f)), reverse=True)
        csv_path = os.path.join(OUTPUTS_DIR, csv_files[0])
        print(f"Auto-detected CSV file: {csv_path}")
    
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    
    df = pd.read_csv(csv_path)
    
    quotes_by_expiry: Dict[str, List[Tuple[float, float, str, Optional[float], str]]] = {}
    
    for _, row in df.iterrows():
        expiry_str = str(row.get('expiry_date', '')).strip()
        if not expiry_str or expiry_str == '' or expiry_str.lower() == 'nan':
            continue
        
        strike = row.get('strike_price_K')
        prob = row.get('probability_p')
        slug = str(row.get('slug', ''))
        question_text = str(row.get('question', '')) if 'question' in row.index else ''
        
        if pd.isna(strike) or pd.isna(prob):
            continue
        
        try:
            k = float(strike)
            p = float(prob)
            q_type, upper_k = parse_question_type_from_slug(slug, k, question=question_text)
            quotes_by_expiry.setdefault(expiry_str, []).append((k, p, q_type, upper_k, slug))
        except (ValueError, TypeError):
            continue
    
    for expiry in quotes_by_expiry:
        quotes_by_expiry[expiry].sort(key=lambda x: x[0])
    
    print(f"\nLoaded quotes from CSV: {csv_path}")
    print(f"Found {len(quotes_by_expiry)} unique expiry dates:")
    for expiry in sorted(quotes_by_expiry.keys()):
        types = {}
        for _, _, qt, _, _ in quotes_by_expiry[expiry]:
            types[qt] = types.get(qt, 0) + 1
        type_str = ", ".join(f"{v} {k}" for k, v in sorted(types.items()))
        print(f"  {expiry}: {len(quotes_by_expiry[expiry])} quotes ({type_str})")
    
    return quotes_by_expiry


def process_single_expiry(
    expiry_iso: str,
    polymarket_quotes: List[Tuple[float, float]],
    valuation_dt: datetime,
    spot: float,
    r_cc: float,
    q_funding: float,
    rates_meta: Dict,
    currency: str = None,
) -> pd.DataFrame:
    """
    Process a single expiry date: build surfaces, run MC, and generate comparison.
    
    Args:
        expiry_iso: Expiry date in YYYY-MM-DD format
        polymarket_quotes: List of (K, probability) tuples for this expiry
        valuation_dt: Valuation datetime
        spot: Current spot price
        r_cc: Risk-free rate (continuous compounding)
        q_funding: Funding rate
        rates_meta: Rates metadata dictionary
        currency: Asset currency ('BTC' or 'ETH'). Defaults to module UNDERLYING.
    
    Returns:
        DataFrame containing flagged opportunities for this expiry (empty if none)
    """
    if currency is None:
        currency = UNDERLYING
    expiry_dt = datetime.strptime(expiry_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    exp_str = expiry_iso.replace('-', '')
    
    print(f"\n{'='*80}")
    print(f"Processing expiry: {expiry_iso}")
    print(f"{'='*80}")
    print(f"Valuation: {valuation_dt:%Y-%m-%d %H:%M:%S} UTC | Target expiry: {expiry_dt:%Y-%m-%d}")
    print(f"Polymarket quotes for this expiry: {len(polymarket_quotes)}")
    
    # Build smiles per expiry
    smiles = {}
    deribit_option_prices = {}  # Initialize to empty dict
    SMILE_SOURCE = "svi_local"
    
    if SMILE_SOURCE.lower() in ("svi_local", "local_svi", "svi"):
        opt_insts = deribit_fetch_option_instruments(currency)
        if not opt_insts:
            raise RuntimeError("Failed to fetch option instruments from Deribit.")
        if not has_deribit_expiry_on_date(opt_insts, expiry_dt):
            raise RuntimeError(
                f"No Deribit option expiry on {expiry_dt.date()} for target expiry {expiry_iso}."
            )
        chosen_expiries = select_expiries_around_target(opt_insts, expiry_dt, max_expiries=6)
        print(f"\nSelected {len(chosen_expiries)} expiries around target for local SVI fit:")
        for d in chosen_expiries:
            print("  ", d.strftime("%Y-%m-%d"))
        fitted_smiles, svi_params, deribit_option_prices = fit_svi_smiles_for_expiries(
            spot=spot, r=r_cc, q=q_funding, valuation_dt=valuation_dt, expiries=chosen_expiries,
            currency=currency,
        )
        smiles = fitted_smiles
        # Save per-expiry SVI params snapshot
        with open(os.path.join(SNAPSHOT_DIR, f"svi_params_local_{currency.lower()}_{exp_str}.json"), "w") as f:
            json.dump(svi_params, f, indent=2)
        # Save raw instruments snapshot
        with open(os.path.join(SNAPSHOT_DIR, f"deribit_options_instruments_{currency.lower()}_{exp_str}.json"), "w") as f:
            json.dump(opt_insts, f)
        # Save Deribit option prices DataFrames
        if deribit_option_prices:
            saved_price_files = []
            for deribit_expiry_iso, price_df in deribit_option_prices.items():
                # Select only Strike, Expiry, Bid_Price, Ask_Price columns
                price_df_output = price_df[["Strike", "Expiry", "Bid_Price", "Ask_Price"]].copy()
                # Sort by Strike
                price_df_output = price_df_output.sort_values("Strike").reset_index(drop=True)
                deribit_exp_str = deribit_expiry_iso.replace('-', '')
                out_csv = os.path.join(DERIBIT_OPTION_PRICES_DIR, f"{currency.lower()}_deribit_options_{deribit_exp_str}.csv")
                price_df_output.to_csv(out_csv, index=False)
                saved_price_files.append(out_csv)
            print(f"Saved {len(saved_price_files)} Deribit option prices CSVs to {DERIBIT_OPTION_PRICES_DIR}")
    
    if not smiles:
        raise RuntimeError("Failed to build any smiles; try a different target or check connectivity.")
    
    # Save per-expiry smiles CSVs
    saved_files = []
    for exp_iso, df in smiles.items():
        df = df[["Strike", "Implied Volatility", "Expiry"]].dropna().sort_values("Strike")
        out_csv = os.path.join(SMILES_DIR, f"{currency.lower()}_smile_{exp_iso.replace('-', '')}.csv")
        df.to_csv(out_csv, index=False)
        saved_files.append(out_csv)
    
    print(f"\nSaved {len(saved_files)} smile CSVs to {SMILES_DIR}")
    
    # Build IV surface from smiles
    smile_files = [p for p in os.listdir(SMILES_DIR) if p.endswith(".csv") and p.startswith(currency.lower()+"_smile_")]
    smiles_loaded: Dict[str, pd.DataFrame] = {}
    for fname in smile_files:
        df = pd.read_csv(os.path.join(SMILES_DIR, fname))
        m = re.search(r"(\d{8})", fname)
        if m:
            exp_iso = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        elif "Expiry" in df.columns:
            exp_iso = str(df["Expiry"].iloc[0])[:10]
        else:
            continue
        smiles_loaded[exp_iso] = df
    
    T_years = yearfrac_365(valuation_dt, expiry_dt)
    if T_years <= 0:
        raise ValueError("Target expiry is in the past.")
    
    ivs = ImpliedVolSurface.from_smile_dict(
        smiles=smiles_loaded,
        spot=spot,
        r=r_cc,
        q=q_funding,
        valuation_dt=valuation_dt,
    )
    
    # Local vol grid bounds
    s_lo = S_MIN_FACTOR * spot
    s_hi = S_MAX_FACTOR * spot
    S_grid = np.exp(np.linspace(np.log(s_lo), np.log(s_hi), 201))
    
    # Time grid
    tmin = max(1e-6, ivs.Ts[0] + 5e-4)
    tmax = min(T_years, ivs.Ts[-1] - 5e-4)
    if tmax <= tmin:
        raise ValueError("Not enough maturity coverage to build local vol. Ensure expiries bracket the target.")
    t_grid = np.linspace(tmin, tmax, 60)
    
    lvs = LocalVolSurface(
        iv_surface=ivs,
        T_end=T_years,
        S_grid=S_grid,
        t_grid=t_grid,
        dK_rel=5e-3,
        dT_abs=5e-4,
        min_var=1e-8,
        max_var=25.0,
    )
    
    mc = LocalVolMC(ivs, lvs, seed=SEED)
    
    # Sweep K values
    K_min = s_lo
    K_max = s_hi
    K_grid = np.linspace(K_min, K_max, SWEEP_POINTS)
    
    M_up, M_down, ST = mc.simulate_touch_envelopes(
        S0=spot,
        T=T_years,
        n_paths=N_PATHS_HIT,
        n_steps=N_STEPS,
        mBTCod=MBTCOD,
        use_mid_sigma=USE_MID_SIGMA,
        seed=SEED,
    )
    
    n = M_up.size
    M_up_sorted = np.sort(M_up)
    M_down_sorted = np.sort(M_down)
    ST_sorted = np.sort(ST)
    
    is_up = K_grid >= spot
    is_down = ~is_up
    p_hit = np.empty_like(K_grid, dtype=float)
    lo = np.empty_like(K_grid, dtype=float)
    hi = np.empty_like(K_grid, dtype=float)
    
    if np.any(is_up):
        idx = M_up_sorted.searchsorted(K_grid[is_up], side="left")
        succ = n - idx
        p = succ / n
        p_hit[is_up] = p
        lo_up, hi_up = wilson_interval_array(p, n, confidence=0.95)
        lo[is_up], hi[is_up] = lo_up, hi_up
    
    if np.any(is_down):
        idx = M_down_sorted.searchsorted(K_grid[is_down], side="right")
        p = idx / n
        p_hit[is_down] = p
        lo_dn, hi_dn = wilson_interval_array(p, n, confidence=0.95)
        lo[is_down], hi[is_down] = lo_dn, hi_dn
    
    p_end_geq_smile = np.array([ivs.tail_probability_from_smile(K, T_years) for K in K_grid])
    idx_end = ST_sorted.searchsorted(K_grid, side="left")
    p_end_geq_mc = (n - idx_end) / n
    p_end_geq_mc_lo, p_end_geq_mc_hi = wilson_interval_array(p_end_geq_mc, n, confidence=0.95)
    
    sweep_df = pd.DataFrame(
        {
            "K": K_grid,
            "direction": np.where(is_up, "up", "down"),
            "p_hit": p_hit,
            "p_hit_lo95": lo,
            "p_hit_hi95": hi,
            "p_end_geq_smile": p_end_geq_smile,
            "p_end_geq_mc": p_end_geq_mc,
            "p_end_geq_mc_lo95": p_end_geq_mc_lo,
            "p_end_geq_mc_hi95": p_end_geq_mc_hi,
            "odds_hit": p_hit / np.maximum(1.0 - p_hit, 1e-12),
        }
    )
    
    sweep_df.insert(0, "S0", spot)
    sweep_df.insert(1, "T_years", T_years)
    
    # Save sweep CSV with expiry-specific filename
    sweep_csv = os.path.join(OUTPUTS_DIR, f"{currency.lower()}_probability_sweep_{exp_str}.csv")
    sweep_df.to_csv(sweep_csv, index=False)
    print(f"Saved sweep CSV to: {sweep_csv}")
    
    # Save meta JSON with expiry-specific filename
    meta_json_path = os.path.join(OUTPUTS_DIR, f"{currency.lower()}_probability_sweep_meta_{exp_str}.json")
    meta_json = {
        "spot": float(spot),
        "expiry_iso": expiry_dt.strftime("%Y-%m-%d"),
        "valuation_dt_iso": valuation_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "T_years": float(T_years),
        "r": float(r_cc),
        "q": float(q_funding),
        "smiles_dir": os.path.abspath(SMILES_DIR),
        "mBTCod": MBTCOD,
        "use_mid_sigma": bool(USE_MID_SIGMA),
        "n_paths": int(N_PATHS_HIT),
        "n_steps": (int(np.ceil(T_years * 365)) if N_STEPS is None else int(N_STEPS)),
        "seed": int(SEED),
        "S_grid_min": float(s_lo),
        "S_grid_max": float(s_hi),
        "S_grid_points": int(len(S_grid)),
        "t_grid_points": int(len(t_grid)),
        "rates_meta": rates_meta,
    }
    with open(meta_json_path, "w") as f:
        json.dump(meta_json, f, indent=2)
    print(f"Saved sweep meta JSON to: {meta_json_path}")
    
    # Comparison with Polymarket quotes using end-of-period probabilities
    # Parse extended quote tuples: (K, prob, question_type, upper_K, slug)
    # or legacy (K, prob) tuples
    pm_K = np.empty(len(polymarket_quotes), dtype=float)
    pm_p = np.empty(len(polymarket_quotes), dtype=float)
    pm_qtypes = []
    pm_upper_K = []
    pm_slugs = []
    for i, q in enumerate(polymarket_quotes):
        pm_K[i] = q[0]
        pm_p[i] = q[1]
        if len(q) >= 5:
            pm_qtypes.append(q[2])
            pm_upper_K.append(q[3])
            pm_slugs.append(q[4])
        else:
            pm_qtypes.append('above')
            pm_upper_K.append(None)
            pm_slugs.append('')

    # Compute model probability for each quote directly from the Deribit
    # terminal digital distribution implied by the call surface.  Predict.fun
    # daily up/down markets resolve on S_T versus the baseline strike; touch
    # probabilities are kept in the sweep only for diagnostics.
    K_vals = sweep_df["K"].values
    p_geq_vals = sweep_df["p_end_geq_smile"].values
    p_geq_lo_vals = sweep_df["p_end_geq_mc_lo95"].values
    p_geq_hi_vals = sweep_df["p_end_geq_mc_hi95"].values

    model_p_at_pmK = np.empty(len(pm_K), dtype=float)
    lo_at_pmK = np.empty(len(pm_K), dtype=float)
    hi_at_pmK = np.empty(len(pm_K), dtype=float)

    for i in range(len(pm_K)):
        qt = pm_qtypes[i]
        if qt == 'between':
            upper = pm_upper_K[i] if pm_upper_K[i] is not None else pm_K[i] + 2000
            p_geq_lower = float(np.interp(pm_K[i], K_vals, p_geq_vals))
            p_geq_upper = float(np.interp(upper, K_vals, p_geq_vals))
            model_p_at_pmK[i] = max(0.0, p_geq_lower - p_geq_upper)
            # CI: count MC paths directly in range for Wilson interval
            count_in_range = int(np.sum((ST >= pm_K[i]) & (ST < upper)))
            lo_at_pmK[i], hi_at_pmK[i] = wilson_interval(count_in_range / n, n, confidence=0.95)
        elif qt == 'below':
            p_geq = float(np.interp(pm_K[i], K_vals, p_geq_vals))
            model_p_at_pmK[i] = 1.0 - p_geq
            p_geq_lo = float(np.interp(pm_K[i], K_vals, p_geq_lo_vals))
            p_geq_hi = float(np.interp(pm_K[i], K_vals, p_geq_hi_vals))
            lo_at_pmK[i] = 1.0 - p_geq_hi
            hi_at_pmK[i] = 1.0 - p_geq_lo
        else:  # 'above'
            model_p_at_pmK[i] = float(np.interp(pm_K[i], K_vals, p_geq_vals))
            lo_at_pmK[i] = float(np.interp(pm_K[i], K_vals, p_geq_lo_vals))
            hi_at_pmK[i] = float(np.interp(pm_K[i], K_vals, p_geq_hi_vals))

    ci_margin_at_pmK = np.minimum(model_p_at_pmK - lo_at_pmK, hi_at_pmK - model_p_at_pmK)
    ci_margin_at_pmK = np.clip(ci_margin_at_pmK, 0.0, 1.0)
    
    spreads = pm_p - model_p_at_pmK
    spreads_pct = spreads * 100.0
    ci_margin_pct_at_pmK = ci_margin_at_pmK * 100.0
    abs_thresh_pct = ABS_THRESHOLD * 100.0
    gate_threshold_pct = np.maximum(ci_margin_pct_at_pmK, abs_thresh_pct)
    flagged = np.abs(spreads_pct) > gate_threshold_pct
    
    # Summary table printout
    if pm_K.size > 0:
        print(f"\nPolymarket vs Deribit terminal model @ {expiry_iso}:")
        header = (
            "K".ljust(12)
            + "Type".ljust(10)
            + "PM".rjust(8)
            + "Model".rjust(10)
            + "Spread".rjust(10)
            + "CI_mrg".rjust(10)
            + "AbsThr".rjust(9)
            + "Flag".rjust(7)
        )
        print(header)
        for i in np.argsort(pm_K):
            qt_label = pm_qtypes[i]
            if qt_label == 'between':
                upper = pm_upper_K[i] if pm_upper_K[i] else pm_K[i] + 2000
                qt_label = f"{int(pm_K[i]/1000)}k-{int(upper/1000)}k"
            print(
                f"{pm_K[i]:,.0f}".ljust(12)
                + f"{qt_label}".ljust(10)
                + f"{pm_p[i]:.3f}".rjust(8)
                + f"{model_p_at_pmK[i]:.3f}".rjust(10)
                + f"{spreads_pct[i]:+.2f}%".rjust(10)
                + f"{ci_margin_pct_at_pmK[i]:.2f}%".rjust(10)
                + f"{abs_thresh_pct:.2f}%".rjust(9)
                + (" YES" if flagged[i] else "  no").rjust(7)
            )
    
    # Generate comparison plot
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 9), constrained_layout=True)
    
    ax1.plot(sweep_df["K"], sweep_df["p_end_geq_mc"], lw=2, label="Model P(S_T ≥ K)", color="#1f77b4")
    ax1.fill_between(sweep_df["K"], sweep_df["p_end_geq_mc_lo95"], sweep_df["p_end_geq_mc_hi95"],
                      color="#1f77b4", alpha=0.18, label="Model 95% CI")
    
    yerr_lower = np.clip(model_p_at_pmK - lo_at_pmK, 0, None)
    yerr_upper = np.clip(hi_at_pmK - model_p_at_pmK, 0, None)
    yerr = np.vstack([yerr_lower, yerr_upper])
    ax1.errorbar(pm_K, model_p_at_pmK, yerr=yerr, fmt="o", color="#1f77b4", ecolor="#1f77b4",
                 elinewidth=1.2, capsize=3, label="Model @ PM K (±95% CI)", alpha=0.9, zorder=3)
    
    ax1.scatter(pm_K[~flagged], pm_p[~flagged], s=60, marker="x", color="#7f7f7f",
                label="Polymarket (not flagged)", zorder=4)
    ax1.scatter(pm_K[flagged], pm_p[flagged], s=70, marker="D", color="#d62728",
                label="Polymarket (flagged)", zorder=5)
    
    ax1.axvline(spot, ls=":", lw=1, color="#444444", label=f"S0 = {spot:,.0f}")
    ax1.set_xlabel("Strike K"); ax1.set_ylabel("Probability")
    ax1.set_title(f"Terminal prob: Deribit model vs Polymarket @ {expiry_iso} (T={T_years:.3f}y)")
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x:.0%}"))
    ax1.grid(True, ls="--", alpha=0.35)
    ax1.legend(loc="best")
    
    ci_margin_curve = np.minimum(
        sweep_df["p_end_geq_mc"] - sweep_df["p_end_geq_mc_lo95"],
        sweep_df["p_end_geq_mc_hi95"] - sweep_df["p_end_geq_mc"]
    ) * 100.0
    ax2.fill_between(sweep_df["K"], -ci_margin_curve, ci_margin_curve, color="#1f77b4",
                      alpha=0.12, label="± Model CI margin")
    ax2.axhline(y=+abs_thresh_pct, color="#ff7f0e", lw=1.5, ls="--",
                label=f"± Abs. threshold ({abs_thresh_pct:.1f}%)")
    ax2.axhline(y=-abs_thresh_pct, color="#ff7f0e", lw=1.5, ls="--")
    ax2.axhline(y=0.0, color="#222222", lw=1.0)
    ax2.scatter(pm_K[~flagged], spreads_pct[~flagged], s=60, color="#7f7f7f", marker="o",
                label="Spread (PM − Model): not flagged", zorder=5)
    ax2.scatter(pm_K[flagged], spreads_pct[flagged], s=70, color="#2ca02c", marker="o",
                label="Spread (PM − Model): flagged", zorder=6)
    ax2.set_xlabel("Strike K"); ax2.set_ylabel("Spread (%)")
    ax2.set_title(f"Spread vs K with gating bands @ {expiry_iso}")
    ax2.grid(True, ls="--", alpha=0.35)
    ax2.legend(loc="best")
    
    caption_left = f"Model valuation: {valuation_dt:%Y-%m-%d %H:%M} | Expiry: {expiry_dt:%Y-%m-%d}"
    caption_right = f"Generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"
    fig.text(0.5, 0.01, f"{caption_left} | {caption_right}", ha="center", va="bottom", fontsize=9)
    
    plot_png = os.path.join(OUTPUTS_DIR, f"polymarket_vs_model_{currency.lower()}_{exp_str}.png")
    fig.savefig(plot_png, dpi=160)
    plt.close()
    print(f"\nSaved comparison figure to: {plot_png}")
    
    # Build Deribit options info string for each Polymarket strike
    deribit_options_info = []
    for pm_k in pm_K:
        options_list = []
        if deribit_option_prices:
            for deribit_expiry_iso, price_df in deribit_option_prices.items():
                price_df_filtered = price_df[
                    (price_df['Strike'] >= pm_k * 0.8) & 
                    (price_df['Strike'] <= pm_k * 1.2)
                ].copy()
                if len(price_df_filtered) > 0:
                    price_df_filtered['Strike_Diff'] = abs(price_df_filtered['Strike'] - pm_k)
                    price_df_filtered = price_df_filtered.nsmallest(5, 'Strike_Diff')
                    for _, row in price_df_filtered.iterrows():
                        strike_str = f"{row['Strike']:,.0f}"
                        expiry_str = row['Expiry']
                        mark_price = f"{row['Mark_Price']:.2f}" if pd.notna(row['Mark_Price']) else "N/A"
                        bid_price = f"{row['Bid_Price']:.2f}" if pd.notna(row['Bid_Price']) else "N/A"
                        ask_price = f"{row['Ask_Price']:.2f}" if pd.notna(row['Ask_Price']) else "N/A"
                        inst_name = row['Instrument_Name']
                        options_list.append(
                            f"{expiry_str}|K={strike_str}|Mark={mark_price}|Bid={bid_price}|Ask={ask_price}|{inst_name}"
                        )
        deribit_options_info.append("; ".join(options_list[:10]) if options_list else "N/A")
    
    # Flagged opportunities
    flag_df = pd.DataFrame({
        "K": pm_K,
        "Question_Type": pm_qtypes,
        "Polymarket": pm_p,
        "Model": model_p_at_pmK,
        "Spread_pct": spreads_pct,
        "CI_margin_pct": ci_margin_pct_at_pmK,
        "Abs_threshold_pct": np.full_like(spreads_pct, abs_thresh_pct),
        "Flagged": flagged,
        "Deribit_Options_Used": deribit_options_info,
    }).loc[np.argsort(pm_K)].reset_index(drop=True)
    
    print(f"\nFlagged opportunities for {expiry_iso}:")
    flagged_df = flag_df[flag_df["Flagged"] == True]
    if len(flagged_df) > 0:
        print(flagged_df[["K", "Question_Type", "Polymarket", "Model", "Spread_pct", "Flagged"]].to_string(index=False))
    else:
        print("  None")
    
    flagged_df = flag_df[flag_df["Flagged"] == True].copy()
    if len(flagged_df) > 0:
        flagged_df["expiry_date"] = expiry_iso
    else:
        flagged_df = pd.DataFrame({
            "K": [], "Question_Type": [], "Polymarket": [], "Model": [],
            "Spread_pct": [], "CI_margin_pct": [], "Abs_threshold_pct": [],
            "Flagged": [], "Deribit_Options_Used": [], "expiry_date": []
        })
    
    return flagged_df


###################
## Main Function ##
###################

def main(polymarket_quotes: Optional[List[Tuple[float, float]]] = None, polymarket_quotes_csv: Optional[str] = None, currency: Optional[str] = None):
    """
    Main function to compare Polymarket vs Model probabilities.
    
    Args:
        polymarket_quotes: Optional list of (K, probability) tuples from Polymarket.
                          If provided, uses single EXPIRY_ISO. If None, loads from CSV.
        polymarket_quotes_csv: Optional path to CSV file. If None, auto-detects latest.
        currency: Asset currency ('BTC' or 'ETH'). Defaults to module UNDERLYING.
    """
    if currency is None:
        currency = UNDERLYING
    # Resolve valuation datetime
    if VALUATION_DT is None:
        valuation_dt = datetime.now(timezone.utc)
    else:
        valuation_dt = datetime.strptime(VALUATION_DT, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    
    # Fetch spot price (shared across all expiries)
    spot = fetch_spot(currency)
    if spot is None:
        raise RuntimeError(f"Failed to fetch {currency} spot price.")
    print(f"Spot {currency}: {spot:,.2f}")
    
    # Determine if we're using CSV mode or single expiry mode
    if polymarket_quotes is not None and EXPIRY_ISO is not None:
        # Single expiry mode: use provided quotes with EXPIRY_ISO
        print(f"\n{'='*80}")
        print("SINGLE EXPIRY MODE")
        print(f"{'='*80}")
        expiry_dt = datetime.strptime(EXPIRY_ISO, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        r_cc, q_funding, rates_meta = get_rates_auto(spot, valuation_dt, expiry_dt, currency)
        print("\nAuto-selected rates")
        print(f"  Risk-free r (CC): {r_cc:.4%}  [{rates_meta.get('r_source')}, raw={rates_meta.get('r_raw_rate', float('nan')):.4%}]")
        used = rates_meta.get("c_meta", {}).get("used", [])
        if used:
            if len(used) == 2:
                print(f"  Carry (r−q) from Deribit (interpolated): used {used[0]['instrument']} (T={used[0]['T']:.3f}y), {used[1]['instrument']} (T={used[1]['T']:.3f}y)")
            else:
                print(f"  Carry (r−q) from Deribit (nearest): used {used[0]['instrument']} (T={used[0]['T']:.3f}y)")
        else:
            print("  Carry (r−q): unavailable; funding fallback used.")
        print(f"  Funding q: {q_funding:.4%}")
        
        # Save rates snapshot
        exp_str = EXPIRY_ISO.replace('-', '')
        with open(os.path.join(SNAPSHOT_DIR, f"rates_{currency.lower()}_{exp_str}.json"), "w") as f:
            json.dump({"spot": spot, "r": r_cc, "q": q_funding, "meta": rates_meta}, f, indent=2)
        
        # Process single expiry
        flagged_df = process_single_expiry(EXPIRY_ISO, polymarket_quotes, valuation_dt, spot, r_cc, q_funding, rates_meta, currency=currency)
        
        # Save flagged opportunities summary
        if len(flagged_df) > 0:
            summary_csv = os.path.join(FLAGGED_DIR, f'flagged_opportunities_summary_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}.csv')
            flagged_df.to_csv(summary_csv, index=False)
            print(f"\nSaved flagged opportunities summary to: {summary_csv}")
        else:
            print("\nNo flagged opportunities found for this expiry.")
        
    else:
        # Multi-expiry mode: load from CSV and process each expiry
        print(f"\n{'='*80}")
        print("MULTI-EXPIRY MODE: Loading quotes from CSV")
        print(f"{'='*80}")
        
        # Load quotes grouped by expiry
        csv_path = polymarket_quotes_csv or POLYMARKET_QUOTES_CSV
        quotes_by_expiry = load_polymarket_quotes_from_csv(csv_path)
        
        if not quotes_by_expiry:
            raise ValueError("No quotes found in CSV file. Please ensure CSV has valid expiry_date and strike_price_K columns.")
        
        # Collect all flagged opportunities across all expiries
        all_flagged_opportunities = []
        
        # Process each expiry date
        for expiry_iso in sorted(quotes_by_expiry.keys()):
            quotes = quotes_by_expiry[expiry_iso]
            if not quotes:
                continue
            
            expiry_dt = datetime.strptime(expiry_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            
            # Get rates for this expiry
            r_cc, q_funding, rates_meta = get_rates_auto(spot, valuation_dt, expiry_dt, currency)
            
            # Process this expiry
            try:
                flagged_df = process_single_expiry(expiry_iso, quotes, valuation_dt, spot, r_cc, q_funding, rates_meta, currency=currency)
                if len(flagged_df) > 0:
                    all_flagged_opportunities.append(flagged_df)
            except Exception as e:
                print(f"\nERROR processing expiry {expiry_iso}: {e}")
                import traceback
                traceback.print_exc()
                print(f"Skipping expiry {expiry_iso} and continuing...\n")
                continue
        
        # Combine all flagged opportunities and save summary CSV
        if all_flagged_opportunities:
            summary_df = pd.concat(all_flagged_opportunities, ignore_index=True)
            # Reorder columns to put expiry_date first
            cols = ["expiry_date", "K", "Question_Type", "Polymarket", "Model", "Spread_pct", "CI_margin_pct", "Abs_threshold_pct", "Deribit_Options_Used", "Flagged"]
            # Only include columns that exist
            cols = [c for c in cols if c in summary_df.columns]
            summary_df = summary_df[cols]
            # Sort by expiry_date, then by K
            summary_df = summary_df.sort_values(["expiry_date", "K"]).reset_index(drop=True)
            
            summary_csv = os.path.join(FLAGGED_DIR, f'flagged_opportunities_summary_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}.csv')
            summary_df.to_csv(summary_csv, index=False)
            
            print(f"\n{'='*80}")
            print("FLAGGED OPPORTUNITIES SUMMARY")
            print(f"{'='*80}")
            print(f"Total flagged opportunities: {len(summary_df)}")
            print(f"Across {summary_df['expiry_date'].nunique()} expiry dates")
            print(f"\nSummary by expiry date:")
            for exp_date in sorted(summary_df['expiry_date'].unique()):
                count = len(summary_df[summary_df['expiry_date'] == exp_date])
                print(f"  {exp_date}: {count} flagged opportunities")
            print(f"\nSaved summary CSV to: {summary_csv}")
        else:
            print(f"\n{'='*80}")
            print("FLAGGED OPPORTUNITIES SUMMARY")
            print(f"{'='*80}")
            print("No flagged opportunities found across all expiry dates.")
        
        print(f"\n{'='*80}")
        print("COMPLETED: Processed all expiry dates")
        print(f"{'='*80}")


if __name__ == '__main__':
    main()
