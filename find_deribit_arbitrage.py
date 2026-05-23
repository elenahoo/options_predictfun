"""
Script to generate Predict.fun quotes CSV and Deribit option prices CSV files.

1. Generates Predict.fun quotes CSV (predictfun_quotes_BTC_YYYYMMDD.csv)
2. Generates Deribit option prices CSV files (btc_deribit_options_YYYYMMDD.csv) for each expiry

# All options together
python find_deribit_arbitrage.py --end-date 2026-02-01 --currency BTC

# Save raw instruments for a specific expiry
python3 find_deribit_arbitrage.py --save-raw-instruments 2026-01-07 BTC

# Show help
python find_deribit_arbitrage.py --help
"""

import pandas as pd
import os
import json
import urllib.parse
import urllib.request
from typing import Optional, List, Dict
from datetime import datetime, timezone

DERIBIT_BASE = 'https://www.deribit.com/api/v2/'

def deribit_api(path: str, params: Dict[str, str]) -> Optional[dict]:
    """Make API call to Deribit."""
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

def fetch_spot_price(currency: str) -> Optional[float]:
    """Fetch current spot price for any currency supported by Deribit/Binance."""
    # Try Deribit index price first (more reliable for options).
    # Deribit index names follow the pattern '{currency_lower}_usd' for all assets.
    index_name = f'{currency.lower()}_usd'
    res = deribit_api('public/get_index_price', {'index_name': index_name})
    if res and 'index_price' in res:
        return float(res['index_price'])

    # Fallback: try Binance spot price.
    # Binance pairs follow the pattern '{CURRENCY}USDT' for all assets.
    symbol = f'{currency.upper()}USDT'
    url = f'https://api.binance.com/api/v3/ticker/price?symbol={symbol}'
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            return float(data.get('price', 0)) or None
    except Exception:
        pass

    return None

def deribit_fetch_option_instruments(currency: str, price_index: Optional[str] = None) -> Optional[List[dict]]:
    """
    Fetch option instruments from Deribit.
    
    Note: When fetching USDC-settled options, we query without currency filter
    because currency=BTC only returns BTC-USD (inverse) options, not BTC-USDC options.
    We then filter by price_index and base_currency instead.
    
    Args:
        currency: Currency code (BTC, ETH, etc.)
        price_index: Optional price index filter (e.g., 'btc_usdc', 'eth_usdc')
    
    Returns:
        List of instrument dictionaries
    """
    # For USDC-settled options, don't use currency filter (it excludes USDC options)
    # Instead, query all options and filter by price_index and base_currency
    if price_index and price_index.endswith('_usdc'):
        # Query all options without currency filter
        params = {'kind': 'option', 'expired': 'false'}
        res = deribit_api('public/get_instruments', params)
        if not res:
            return None
        
        # Filter by price_index and base_currency
        currency_upper = currency.upper()
        filtered = [
            inst for inst in res
            if inst.get('price_index', '').lower() == price_index.lower()
            and inst.get('base_currency', '').upper() == currency_upper
        ]
        return filtered
    else:
        # For non-USDC options, use currency filter as usual
        params = {'currency': currency, 'kind': 'option', 'expired': 'false'}
        if price_index:
            params['price_index'] = price_index
        res = deribit_api('public/get_instruments', params)
        if not res:
            return None
        return res

def fetch_deribit_options_for_expiry(currency: str, expiry_dt: datetime) -> pd.DataFrame:
    """
    Fetch CALL options only for USDC-settled options for a specific expiry date.
    
    Filters for:
    - Call options only (option_type == 'call')
    - USDC-settled options (settlement_currency == 'USDC')
    - Price index matching currency (e.g., 'btc_usdc' for BTC, 'eth_usdc' for ETH)
    
    Prices are already in USDC (USD), no conversion needed.
    
    Returns DataFrame with columns: Strike, Expiry, Bid_Price, Ask_Price (in USD/USDC)
    """
    # Set price_index based on currency
    currency_upper = currency.upper()
    if currency_upper == 'BTC':
        price_index = 'btc_usdc'
    elif currency_upper == 'ETH':
        price_index = 'eth_usdc'
    else:
        # Default: try currency_usdc format
        price_index = f'{currency.lower()}_usdc'
    
    print(f"  Fetching USDC-settled options with price_index: {price_index}")
    
    # Fetch USDC-settled instruments (pass price_index to use special query logic)
    insts = deribit_fetch_option_instruments(currency, price_index=price_index)
    if not insts:
        print(f"  Warning: No option instruments fetched from Deribit")
        return pd.DataFrame()
    
    print(f"  Fetched {len(insts)} USDC-settled option instruments from Deribit (price_index={price_index})")
    
    # Filter for USDC settlement, matching price_index, and linear instrument type
    # USDC-settled options have:
    # - price_index == '{currency}_usdc' (e.g., 'btc_usdc')
    # - settlement_currency == 'USDC'
    # - instrument_type == 'linear' (not 'reversed' which are inverse options)
    usdc_insts = []
    price_index_mismatch = 0
    settlement_mismatch = 0
    instrument_type_mismatch = 0
    
    for inst in insts:
        inst_price_index = inst.get('price_index', '').lower()
        settlement_currency = inst.get('settlement_currency', '').upper()
        instrument_type = inst.get('instrument_type', '').lower()
        
        # Debug: count mismatches
        if inst_price_index != price_index:
            price_index_mismatch += 1
            continue
        if settlement_currency != 'USDC':
            settlement_mismatch += 1
            continue
        if instrument_type != 'linear':
            instrument_type_mismatch += 1
            continue
        
        # All filters passed
        usdc_insts.append(inst)
    
    print(f"  Filtered to {len(usdc_insts)} USDC-settled linear instruments")
    print(f"    - Price index mismatch: {price_index_mismatch}")
    print(f"    - Settlement currency mismatch: {settlement_mismatch}")
    print(f"    - Instrument type mismatch (not linear): {instrument_type_mismatch}")
    
    if not usdc_insts:
        print(f"  Warning: No USDC-settled options found for {currency}")
        print(f"    Looking for: price_index={price_index}, settlement_currency=USDC, instrument_type=linear")
        # Debug: show what we actually got
        if insts:
            sample = insts[0]
            print(f"    Sample instrument: price_index={sample.get('price_index')}, "
                  f"settlement_currency={sample.get('settlement_currency')}, "
                  f"instrument_type={sample.get('instrument_type')}")
        return pd.DataFrame()
    
    insts = usdc_insts  # Use filtered list
    
    # Deribit options expire at 8:00 UTC on the expiry date
    # Match the exact timestamp (should already be set to 8:00 UTC)
    exp_ms = int(expiry_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    expiry_str = expiry_dt.strftime('%Y-%m-%d')
    
    rows = []
    matched_count = 0
    ticker_failed_count = 0
    no_price_count = 0
    filtered_out_count = 0
    usdc_count = 0
    
    for inst in insts:
        inst_exp_ms = inst.get('expiration_timestamp')
        strike = inst.get('strike')
        option_type = inst.get('option_type', '').lower()
        name = inst.get('instrument_name', '')
        
        # Match expiry timestamp exactly
        if inst_exp_ms != exp_ms or strike is None:
            continue
        
        # Filter: Only CALL options
        if option_type != 'call':
            filtered_out_count += 1
            continue
        
        # Settlement currency should already be filtered, but double-check
        settlement_currency = inst.get('settlement_currency', '').upper()
        if settlement_currency != 'USDC':
            filtered_out_count += 1
            continue
        
        matched_count += 1
        
        if not name:
            continue
        
        tick = deribit_api('public/ticker', {'instrument_name': name})
        if not tick:
            ticker_failed_count += 1
            continue
        
        # Get bid/ask prices (allow None values, matching working code)
        bid_price_raw = tick.get('best_bid_price') or tick.get('bid_price')
        ask_price_raw = tick.get('best_ask_price') or tick.get('ask_price')
        
        # Only skip if BOTH are None (allow one to be None)
        if bid_price_raw is None and ask_price_raw is None:
            no_price_count += 1
            continue
        
        # BTC-USDC options are settled in USDC, so prices are already in USD/USDC
        # No conversion needed
        bid_price_usd = float(bid_price_raw) if bid_price_raw is not None else None
        ask_price_usd = float(ask_price_raw) if ask_price_raw is not None else None
        
        rows.append({
            'Strike': float(strike),
            'Expiry': expiry_str,
            'Bid_Price': bid_price_usd,
            'Ask_Price': ask_price_usd
        })
    
    print(f"  Matched {matched_count} USDC-settled call options for expiry {expiry_str}")
    print(f"  Filtered out {filtered_out_count} non-call options")
    print(f"  Ticker API failed for {ticker_failed_count} instruments")
    print(f"  No bid/ask prices for {no_price_count} instruments")
    print(f"  Successfully processed {len(rows)} USDC-settled call options (prices in USDC/USD)")
    
    if not rows:
        return pd.DataFrame()
    
    df = pd.DataFrame(rows).sort_values('Strike')
    return df

def generate_predictfun_csv(currency: str = 'BTC', end_date: Optional[datetime] = None):
    """
    Generate the Predict.fun quotes CSV by calling fetch_predictfun_prob.py functionality.
    
    Args:
        currency: Currency to fetch quotes for (default: "BTC")
        end_date: Optional datetime to filter markets ending before this date
    
    Returns path to generated CSV file, or None if error.
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from fetch_predictfun_prob import fetch_predictfun_quotes
        
        print(f"Generating Predict.fun quotes CSV for {currency}...")
        if end_date:
            print(f"  End date filter: {end_date.strftime('%Y-%m-%d')}")
        
        quotes = fetch_predictfun_quotes(currency=currency, end_date=end_date)
        
        if not quotes:
            print("Error: No quotes fetched from Predict.fun")
            return None
        
        outputs_dir = os.path.join(os.getcwd(), 'outputs')
        os.makedirs(outputs_dir, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        csv_path = os.path.join(outputs_dir, f'predictfun_quotes_{currency}_{timestamp}.csv')
        
        import csv
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['strike_price_K', 'probability_p', 'bestAsk', 'bestBid', 'lastTradePrice', 'expiry_date', 'slug', 'predictfun_url', 'updatedAt', 'question'])
            
            for q in quotes:
                k, p, expiry, slug, url = q[0], q[1], q[2], q[3], q[4]
                ba, bb, ltp, ua = q[5], q[6], q[7], q[8]
                question = q[9] if len(q) > 9 else ""
                expiry_str = expiry.strftime("%Y-%m-%d") if expiry else ""
                best_ask_str = str(ba) if ba is not None else ""
                best_bid_str = str(bb) if bb is not None else ""
                last_trade_str = str(ltp) if ltp is not None else ""
                updated_at_str = str(ua) if ua else ""
                writer.writerow([k, p, best_ask_str, best_bid_str, last_trade_str, expiry_str, slug, url, updated_at_str, question])
        
        print(f"Generated CSV: {csv_path}")
        return csv_path
        
    except Exception as e:
        print(f"Error generating CSV: {e}")
        import traceback
        traceback.print_exc()
        return None

def generate_deribit_option_prices_csv(predictfun_csv_path: Optional[str] = None, currency: str = 'BTC'):
    """
    Generate Deribit option prices CSV files for all expiries found in Predict.fun CSV.
    
    Args:
        predictfun_csv_path: Path to Predict.fun quotes CSV. If None, auto-detects latest.
        currency: Currency for Deribit options (default: 'BTC')
    
    Returns:
        List of paths to generated CSV files.
    """
    if predictfun_csv_path is None:
        outputs_dir = os.path.join(os.getcwd(), 'outputs')
        if os.path.exists(outputs_dir):
            csv_files = [f for f in os.listdir(outputs_dir) if f.startswith('predictfun_quotes_') and f.endswith('.csv')]
            if csv_files:
                csv_files.sort(key=lambda f: os.path.getmtime(os.path.join(outputs_dir, f)), reverse=True)
                predictfun_csv_path = os.path.join(outputs_dir, csv_files[0])
                print(f"Auto-detected Predict.fun CSV: {predictfun_csv_path}")
    
    if predictfun_csv_path is None or not os.path.exists(predictfun_csv_path):
        print(f"Error: Predict.fun CSV file not found: {predictfun_csv_path}")
        return []
    
    df = pd.read_csv(predictfun_csv_path)
    
    expiry_dates = df['expiry_date'].dropna().unique()
    
    if len(expiry_dates) == 0:
        print("Error: No expiry dates found in Predict.fun CSV")
        return []
    
    print(f"\nFound {len(expiry_dates)} unique expiry dates")
    
    # Create deribit_option_prices directory
    deribit_option_prices_dir = os.path.join(os.getcwd(), 'deribit_option_prices')
    os.makedirs(deribit_option_prices_dir, exist_ok=True)
    
    saved_files = []
    
    print(f"Using currency: {currency}")
    
    for expiry_str in expiry_dates:
        if pd.isna(expiry_str) or not expiry_str.strip():
            continue
        
        try:
            # Parse expiry date
            # Deribit options expire at 8:00 UTC, so set time to 8:00 UTC
            expiry_dt = datetime.strptime(str(expiry_str).strip(), '%Y-%m-%d').replace(
                hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
            )
            
            print(f"\nFetching Deribit options for expiry: {expiry_str} (8:00 UTC)")
            deribit_options_df = fetch_deribit_options_for_expiry(currency, expiry_dt)
            
            if deribit_options_df.empty:
                print(f"  No Deribit options found for expiry {expiry_str}")
                continue
            
            # Select only Strike, Expiry, Bid_Price, Ask_Price columns
            price_df_output = deribit_options_df[["Strike", "Expiry", "Bid_Price", "Ask_Price"]].copy()
            # Sort by Strike
            price_df_output = price_df_output.sort_values("Strike").reset_index(drop=True)
            
            # Generate filename
            expiry_str_clean = expiry_str.replace('-', '')
            out_csv = os.path.join(deribit_option_prices_dir, f"{currency.lower()}_deribit_options_{expiry_str_clean}.csv")
            
            # Save to CSV
            price_df_output.to_csv(out_csv, index=False)
            saved_files.append(out_csv)
            print(f"  Saved {len(price_df_output)} options to: {out_csv}")
            
        except ValueError as e:
            print(f"  Error parsing expiry date '{expiry_str}': {e}")
            continue
        except Exception as e:
            print(f"  Error processing expiry {expiry_str}: {e}")
            continue
    
    print(f"\nSaved {len(saved_files)} Deribit option prices CSVs to {deribit_option_prices_dir}")
    return saved_files

def combine_deribit_csvs(deribit_option_prices_dir: Optional[str] = None, currency: str = 'BTC') -> pd.DataFrame:
    """
    Combine all Deribit option prices CSV files into a single DataFrame.
    
    Args:
        deribit_option_prices_dir: Directory containing Deribit CSV files. If None, uses default.
        currency: Currency prefix to filter CSV files (default: 'BTC')
    
    Returns:
        Combined DataFrame with columns: Strike, Expiry, Bid_Price, Ask_Price
    """
    if deribit_option_prices_dir is None:
        deribit_option_prices_dir = os.path.join(os.getcwd(), 'deribit_option_prices')
    
    if not os.path.exists(deribit_option_prices_dir):
        print(f"Warning: Deribit option prices directory not found: {deribit_option_prices_dir}")
        return pd.DataFrame()
    
    # Find all CSV files in the directory matching the currency pattern
    currency_lower = currency.lower()
    csv_files = [f for f in os.listdir(deribit_option_prices_dir) 
                 if f.endswith('.csv') and f.startswith(f'{currency_lower}_deribit_options_')]
    
    if not csv_files:
        print(f"Warning: No Deribit CSV files found in {deribit_option_prices_dir}")
        return pd.DataFrame()
    
    print(f"Found {len(csv_files)} Deribit CSV files to combine")
    
    # Read and combine all CSV files
    dfs = []
    for csv_file in csv_files:
        csv_path = os.path.join(deribit_option_prices_dir, csv_file)
        try:
            df = pd.read_csv(csv_path)
            if not df.empty:
                dfs.append(df)
                print(f"  Loaded {len(df)} rows from {csv_file}")
        except Exception as e:
            print(f"  Error reading {csv_file}: {e}")
            continue
        
    if not dfs:
        print("Warning: No data loaded from Deribit CSV files")
        return pd.DataFrame()
    
    # Combine all DataFrames
    combined_df = pd.concat(dfs, ignore_index=True)
    
    # Handle duplicate strikes for same expiry (calls and puts)
    # Aggregate by taking the average of bid/ask prices
    # This handles cases where there are both call and put options for the same strike
    combined_df = combined_df.groupby(['Strike', 'Expiry']).agg({
        'Bid_Price': 'mean',
        'Ask_Price': 'mean'
    }).reset_index()
    
    print(f"Combined into {len(combined_df)} unique strike/expiry combinations")
    
    return combined_df

def combine_predictfun_deribit_csvs(
    predictfun_csv_path: str,
    deribit_option_prices_dir: Optional[str] = None,
    output_csv_path: Optional[str] = None,
    currency: str = 'BTC'
) -> str:
    """
    Combine Predict.fun quotes CSV with Deribit option prices CSV.
    
    Left joins Deribit data to Predict.fun data on:
    - strike_price_K (predictfun) = Strike (deribit)
    - expiry_date (predictfun) = Expiry (deribit)
    
    Args:
        predictfun_csv_path: Path to Predict.fun quotes CSV
        deribit_option_prices_dir: Directory containing Deribit CSV files. If None, uses default.
        output_csv_path: Path for output CSV. If None, generates automatically.
    
    Returns:
        Path to the combined CSV file
    """
    print(f"Reading Predict.fun CSV: {predictfun_csv_path}")
    predictfun_df = pd.read_csv(predictfun_csv_path)
    print(f"  Loaded {len(predictfun_df)} rows from Predict.fun CSV")
    
    deribit_df = combine_deribit_csvs(deribit_option_prices_dir, currency=currency)
    
    if deribit_df.empty:
        print("Warning: No Deribit data available. Output will only contain Predict.fun data.")
        deribit_df = pd.DataFrame(columns=['Strike', 'Expiry', 'Bid_Price', 'Ask_Price'])
    
    predictfun_df['strike_price_K'] = predictfun_df['strike_price_K'].astype(float)
    predictfun_df['expiry_date'] = pd.to_datetime(predictfun_df['expiry_date']).dt.strftime('%Y-%m-%d')
    
    deribit_df['Strike'] = deribit_df['Strike'].astype(float)
    deribit_df['Expiry'] = pd.to_datetime(deribit_df['Expiry']).dt.strftime('%Y-%m-%d')
    
    merged_df = predictfun_df.merge(
        deribit_df[['Strike', 'Expiry', 'Bid_Price', 'Ask_Price']],
        left_on=['strike_price_K', 'expiry_date'],
        right_on=['Strike', 'Expiry'],
        how='left',
        suffixes=('', '_deribit')
    )
    
    merged_df = merged_df.drop(columns=['Strike', 'Expiry'], errors='ignore')
    merged_df = merged_df.rename(columns={
        'Bid_Price': 'deribit_bid_price',
        'Ask_Price': 'deribit_ask_price'
    })
    
    if output_csv_path is None:
        outputs_dir = os.path.join(os.getcwd(), 'outputs')
        os.makedirs(outputs_dir, exist_ok=True)
        base_name = os.path.basename(predictfun_csv_path)
        base_name_no_ext = os.path.splitext(base_name)[0]
        output_csv_path = os.path.join(outputs_dir, f'{base_name_no_ext}_combined.csv')
    
    merged_df.to_csv(output_csv_path, index=False)
    
    matched_count = merged_df['deribit_bid_price'].notna().sum()
    print(f"\nMerge statistics:")
    print(f"  Total Predict.fun rows: {len(merged_df)}")
    print(f"  Rows with Deribit data: {matched_count}")
    print(f"  Rows without Deribit data: {len(merged_df) - matched_count}")
    print(f"  Match rate: {matched_count/len(merged_df)*100:.1f}%")
    
    print(f"\nSaved combined CSV to: {output_csv_path}")
    
    return output_csv_path

def parse_arguments():
    """
    Parse command-line arguments.
    
    Returns:
        tuple: (end_date, predictfun_csv, currency, skip_predictfun, skip_deribit, skip_combine)
    """
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Generate Predict.fun quotes CSV and Deribit option prices CSV files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate both CSVs with default settings
  python find_deribit_arbitrage.py
  
  # Filter by end date
  python find_deribit_arbitrage.py --end-date 2026-02-01
  
  # Use existing Predict.fun CSV (skip generation)
  python find_deribit_arbitrage.py --predictfun-csv outputs/predictfun_quotes_BTC_20260105.csv
  
  # Only generate Predict.fun CSV (skip Deribit)
  python find_deribit_arbitrage.py --skip-deribit
  
  # Only generate Deribit CSVs (skip Predict.fun)
  python find_deribit_arbitrage.py --skip-predictfun --predictfun-csv outputs/predictfun_quotes_BTC_20260105.csv
  
  # Skip combining CSVs (only generate separate files)
  python find_deribit_arbitrage.py --skip-combine
  
  # All options together
  python find_deribit_arbitrage.py --end-date 2026-02-01 --currency BTC
        """
    )
    
    parser.add_argument(
        '--end-date', '-e',
        type=str,
        default=None,
        help='Filter markets ending before this date (YYYY-MM-DD format)'
    )
    
    parser.add_argument(
        '--predictfun-csv', '-c',
        type=str,
        default=None,
        help='Path to existing Predict.fun CSV file (skip generation if provided)'
    )
    
    parser.add_argument(
        '--currency', '--curr',
        type=str,
        default='BTC',
        choices=['BTC', 'ETH'],
        help='Currency for Deribit options (default: BTC)'
    )
    
    parser.add_argument(
        '--skip-predictfun',
        action='store_true',
        help='Skip Predict.fun CSV generation (requires --predictfun-csv)'
    )
    
    parser.add_argument(
        '--skip-deribit',
        action='store_true',
        help='Skip Deribit option prices CSV generation'
    )
    
    parser.add_argument(
        '--skip-combine',
        action='store_true',
        help='Skip combining Predict.fun and Deribit CSVs (Step 3)'
    )
    
    args = parser.parse_args()
    
    end_date = None
    if args.end_date:
        try:
            end_date = datetime.strptime(args.end_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            parser.error(f"Invalid end date format: {args.end_date}. Use YYYY-MM-DD format.")
    
    if args.skip_predictfun and not args.predictfun_csv:
        parser.error("--skip-predictfun requires --predictfun-csv to be provided")
    
    return end_date, args.predictfun_csv, args.currency, args.skip_predictfun, args.skip_deribit, args.skip_combine

def main():
    """
    Main function to generate both CSV files.
    """
    import sys
    
    end_date, predictfun_csv_path, currency, skip_predictfun, skip_deribit, skip_combine = parse_arguments()
    
    predictfun_csv = None
    
    if not skip_predictfun:
        print("=" * 80)
        print("STEP 1: Generating Predict.fun quotes CSV")
        print("=" * 80)
        
        if predictfun_csv_path and os.path.exists(predictfun_csv_path):
            print(f"Using provided Predict.fun CSV: {predictfun_csv_path}")
            predictfun_csv = predictfun_csv_path
        else:
            predictfun_csv = generate_predictfun_csv(currency=currency, end_date=end_date)
            
            if predictfun_csv is None:
                print("Error: Could not generate Predict.fun CSV")
                if skip_deribit:
                    return
                print("Attempting to use auto-detected CSV for Deribit generation...")
    else:
        print("Skipping Predict.fun CSV generation (--skip-predictfun)")
        if predictfun_csv_path:
            predictfun_csv = predictfun_csv_path
            if not os.path.exists(predictfun_csv):
                print(f"Error: Provided Predict.fun CSV not found: {predictfun_csv}")
                return
    
    if not skip_deribit:
        print("\n" + "=" * 80)
        print("STEP 2: Generating Deribit option prices CSV files")
        print("=" * 80)
        
        if predictfun_csv is None:
            print("Error: No Predict.fun CSV available for Deribit generation")
            return
        
        deribit_csvs = generate_deribit_option_prices_csv(predictfun_csv_path=predictfun_csv, currency=currency)
    else:
        deribit_csvs = []
        print("Skipped Deribit CSV generation (--skip-deribit)")
    
    combined_csv = None
    if not skip_combine:
        print("\n" + "=" * 80)
        print("STEP 3: Combining Predict.fun and Deribit CSVs")
        print("=" * 80)
        
        if predictfun_csv is None:
            print("Error: No Predict.fun CSV available for combining")
        else:
            combined_csv = combine_predictfun_deribit_csvs(
                predictfun_csv_path=predictfun_csv,
                currency=currency
            )
    
    print("\n" + "=" * 80)
    print("Completed!")
    if predictfun_csv:
        print(f"Predict.fun CSV: {predictfun_csv}")
    if not skip_deribit:
        print(f"Deribit CSVs: {len(deribit_csvs)} files")
    if not skip_combine and combined_csv:
        print(f"Combined CSV: {combined_csv}")
    elif skip_combine:
        print("Skipped CSV combination (--skip-combine)")
    print("=" * 80)

def save_raw_instruments_for_expiry(expiry_date_str: str, currency: str = 'BTC', output_file: Optional[str] = None):
    """
    Fetch and save all raw option instruments from Deribit for a specific expiry date.
    Useful for debugging and investigating instrument structure.
    
    Args:
        expiry_date_str: Expiry date in 'YYYY-MM-DD' format
        currency: Currency (default: 'BTC')
        output_file: Output CSV path (default: auto-generated)
    """
    import csv
    import json
    
    # Parse expiry date
    expiry_dt = datetime.strptime(expiry_date_str, '%Y-%m-%d').replace(
        hour=8, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
    )
    exp_ms = int(expiry_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    
    print(f"Fetching all option instruments for expiry: {expiry_date_str} (8:00 UTC)")
    print(f"Target timestamp: {exp_ms}")
    
    # Fetch all instruments
    insts = deribit_fetch_option_instruments(currency)
    if not insts:
        print("Error: No option instruments fetched from Deribit")
        return
    
    print(f"Fetched {len(insts)} total option instruments")
    
    # Filter for matching expiry
    matching_insts = []
    for inst in insts:
        inst_exp_ms = inst.get('expiration_timestamp')
        if inst_exp_ms == exp_ms:
            matching_insts.append(inst)
    
    print(f"Found {len(matching_insts)} instruments matching expiry {expiry_date_str}")
    
    if not matching_insts:
        print("No matching instruments found")
        return
    
    # Collect all unique field names
    all_fields = set()
    for inst in matching_insts:
        all_fields.update(inst.keys())
    
    # Sort fields for consistent CSV columns
    field_names = sorted(all_fields)
    
    # Generate output filename
    if output_file is None:
        expiry_clean = expiry_date_str.replace('-', '')
        output_file = os.path.join(os.getcwd(), f'deribit_raw_instruments_{currency.lower()}_{expiry_clean}.csv')
    
    # Write to CSV
    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=field_names, extrasaction='ignore')
        writer.writeheader()
        
        for inst in matching_insts:
            # Convert all values to strings for CSV
            row = {}
            for field in field_names:
                value = inst.get(field)
                if value is None:
                    row[field] = ''
                elif isinstance(value, (dict, list)):
                    row[field] = json.dumps(value)
                else:
                    row[field] = str(value)
            writer.writerow(row)
    
    print(f"Saved {len(matching_insts)} instruments to: {output_file}")
    print(f"Fields included: {', '.join(field_names)}")
    
    return output_file

if __name__ == '__main__':
    import sys
    
    # Check if user wants to save raw instruments
    if len(sys.argv) > 1 and sys.argv[1] == '--save-raw-instruments':
        if len(sys.argv) < 3:
            print("Usage: python find_deribit_arbitrage.py --save-raw-instruments YYYY-MM-DD [currency]")
            print("Example: python find_deribit_arbitrage.py --save-raw-instruments 2026-01-07 BTC")
            sys.exit(1)
        
        expiry_date = sys.argv[2]
        currency = sys.argv[3] if len(sys.argv) > 3 else 'BTC'
        
        save_raw_instruments_for_expiry(expiry_date, currency)
    else:
        main()
