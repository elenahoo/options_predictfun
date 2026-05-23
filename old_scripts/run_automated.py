#!/usr/bin/env python3
"""
Automated runner for Polymarket vs Deribit options comparison.
Runs every 15 minutes to:
1. Fetch latest Polymarket BTC prediction markets (fetch_polymarket_prob.py)
2. Compare Polymarket vs Deribit probabilities for all expiries (price_option_all_expiries.py)

python run_automated.py --search-terms "btc,bitcoin" --end-date 2026-04-02 --once
python run_automated.py --search-terms "btc,bitcoin" --end-date 2026-04-02 --frequency 10
python run_automated.py --search-terms "btc,bitcoin" --end-date 2026-04-02 --frequency 1h
"""

import os
import sys
import time
import logging
import traceback
import csv
from datetime import datetime, timezone
from typing import List, Tuple, Optional

# Add current directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import schedule
except ImportError:
    print("ERROR: 'schedule' library not installed. Install with: pip install schedule")
    sys.exit(1)

from price_option_all_expiries import main as price_main, BASE_DIR, OUTPUTS_DIR
from fetch_polymarket_prob import fetch_polymarket_quotes_for_btc

# Configure logging
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

log_file = os.path.join(LOG_DIR, f'automated_run_{datetime.now(timezone.utc).strftime("%Y%m%d")}.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

# Configuration for automated runs (can be overridden via command line)
DEFAULT_SEARCH_TERMS = ["BTC", "bitcoin"]
DEFAULT_END_DATE = None  # Set to a datetime object if you want to filter by end date
DEFAULT_POLYMARKET_CSV = None  # Path to CSV file (None = auto-detect latest)
DEFAULT_FREQUENCY_MINUTES = 15  # Default run frequency in minutes


def parse_arguments():
    """
    Parse command-line arguments.
    
    Returns:
        tuple: (search_terms, end_date, polymarket_csv, run_once, frequency_minutes)
    """
    search_terms = DEFAULT_SEARCH_TERMS
    end_date = DEFAULT_END_DATE
    polymarket_csv = DEFAULT_POLYMARKET_CSV
    run_once = False
    frequency_minutes = DEFAULT_FREQUENCY_MINUTES
    
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        
        # Search terms
        if arg in ['--search-terms', '--search', '-s']:
            if i + 1 < len(sys.argv):
                search_terms_str = sys.argv[i + 1]
                search_terms = [term.strip() for term in search_terms_str.split(',') if term.strip()]
                i += 2
                continue
        
        # End date
        if arg in ['--end-date', '--endDate', '-e']:
            if i + 1 < len(sys.argv):
                end_date_str = sys.argv[i + 1]
                try:
                    if 'T' in end_date_str:
                        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    else:
                        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError as e:
                    logger.error(f"Error parsing end date '{end_date_str}': {e}")
                    logger.error("Please use format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
                    sys.exit(1)
                i += 2
                continue
        
        # CSV path for price_option_all_expiries.py
        if arg in ['--csv', '-c']:
            if i + 1 < len(sys.argv):
                polymarket_csv = sys.argv[i + 1]
                i += 2
                continue
        
        # Run once (don't schedule)
        if arg in ['--once', '--run-once', '-o']:
            run_once = True
            i += 1
            continue
        
        # Frequency/Interval
        if arg in ['--frequency', '--interval', '-f']:
            if i + 1 < len(sys.argv):
                freq_str = sys.argv[i + 1].lower()
                try:
                    # Support formats: "15", "30", "60", "1h", "2h", etc.
                    if freq_str.endswith('h'):
                        frequency_minutes = int(freq_str[:-1]) * 60
                    elif freq_str.endswith('m'):
                        frequency_minutes = int(freq_str[:-1])
                    else:
                        frequency_minutes = int(freq_str)
                    
                    # Validate common frequencies
                    if frequency_minutes not in [15, 30, 60]:
                        logger.warning(f"Frequency {frequency_minutes} minutes is not a standard option (15, 30, 60), but will be used anyway.")
                except ValueError:
                    logger.error(f"Invalid frequency format: {freq_str}")
                    logger.error("Use format: 15, 30, 60 (minutes) or 1h, 2h (hours)")
                    sys.exit(1)
                i += 2
                continue
        
        # Help
        if arg in ['--help', '-h']:
            print("""
Usage: python run_automated.py [OPTIONS]

Options:
  --search-terms, --search, -s TERMS
                        Comma-separated search terms (e.g., "btc,bitcoin")
                        Default: "BTC,bitcoin"
  
  --end-date, --endDate, -e DATE
                        Filter markets ending before this date (YYYY-MM-DD)
                        Default: None (no filter)
  
  --csv, -c PATH        Path to Polymarket quotes CSV file
                        Default: None (auto-detect latest in outputs/)
  
  --once, --run-once, -o
                        Run once and exit (don't schedule)
                        Default: Schedule every 15 minutes
  
  --frequency, --interval, -f MINUTES
                        Run frequency in minutes (15, 30, 60) or hours (1h, 2h)
                        Default: 15 minutes
                        Options: 15, 30, 60 (or 1h)
  
  --help, -h            Show this help message

Examples:
  # Run with default settings (scheduled every 15 minutes)
  python run_automated.py
  
  # Run every 30 minutes
  python run_automated.py --frequency 30
  
  # Run every hour
  python run_automated.py --frequency 60
  # or
  python run_automated.py --frequency 1h
  
  # Run once with custom search terms
  python run_automated.py --search-terms "btc,bitcoin" --once
  
  # Run with end date filter
  python run_automated.py --end-date 2027-01-02 --once
  
  # Run with specific CSV file
  python run_automated.py --csv outputs/polymarket_quotes_btc_bitcoin_20260102.csv --once
  
  # All options together
  python run_automated.py --search-terms "btc,bitcoin" --end-date 2027-01-02 --once
  
  # Scheduled run every 30 minutes with custom search terms
  python run_automated.py --search-terms "btc,bitcoin" --frequency 30
            """)
            sys.exit(0)
        
        # Unknown argument
        logger.warning(f"Unknown argument: {arg}")
        i += 1
    
    return search_terms, end_date, polymarket_csv, run_once, frequency_minutes


def run_fetch_polymarket(search_terms: List[str], end_date: Optional[datetime]):
    """
    Step 1: Fetch Polymarket quotes and save to CSV.
    Equivalent to running fetch_polymarket_prob.py in default mode.
    """
    logger.info("-" * 80)
    logger.info("STEP 1: Fetching Polymarket quotes")
    logger.info("-" * 80)
    
    try:
        # Fetch quotes using provided search terms
        logger.info(f"Fetching Polymarket quotes for search terms: {search_terms}")
        quotes = fetch_polymarket_quotes_for_btc(
            search_terms=search_terms,
            end_date=end_date
        )
        
        if not quotes:
            logger.warning("No Polymarket quotes found.")
            return None
        
        logger.info(f"Found {len(quotes)} Polymarket quotes")
        
        # Log first few quotes
        for q in quotes[:5]:
            k, p, expiry, slug = q[0], q[1], q[2], q[3]
            expiry_str = expiry.strftime("%Y-%m-%d") if expiry else "N/A"
            logger.info(f"  K={k:,.0f}, p={p:.3f}, expiry={expiry_str}, slug={slug}")
        if len(quotes) > 5:
            logger.info(f"  ... and {len(quotes) - 5} more")
        
        # Write quotes to CSV file (same logic as fetch_polymarket_prob.py)
        search_str = "_".join(search_terms).replace(" ", "_")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        quotes_csv_output = f"polymarket_quotes_{search_str}_{timestamp}.csv"
        
        # Ensure outputs directory exists
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        quotes_csv_output = os.path.join(OUTPUTS_DIR, quotes_csv_output)
        
        with open(quotes_csv_output, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['strike_price_K', 'probability_p', 'bestAsk', 'bestBid', 'lastTradePrice', 'expiry_date', 'slug', 'polymarket_url', 'updatedAt', 'question'])
            
            for q in quotes:
                k, p, expiry, slug, url = q[0], q[1], q[2], q[3], q[4]
                ba, bb, ltp, ua = q[5], q[6], q[7], q[8]
                question = q[9] if len(q) > 9 else ""
                expiry_str = expiry.strftime("%Y-%m-%d") if expiry else ""
                # Convert None values to empty strings for CSV
                best_ask_str = str(ba) if ba is not None else ""
                best_bid_str = str(bb) if bb is not None else ""
                last_trade_str = str(ltp) if ltp is not None else ""
                updated_at_str = str(ua) if ua else ""
                writer.writerow([k, p, best_ask_str, best_bid_str, last_trade_str, expiry_str, slug, url, updated_at_str, question])
        
        logger.info(f"Quotes written to CSV file: {quotes_csv_output}")
        return quotes_csv_output
        
    except Exception as e:
        logger.error(f"Error fetching Polymarket quotes: {e}")
        logger.error(traceback.format_exc())
        return None


def run_price_comparison(polymarket_csv: Optional[str] = None):
    """
    Step 2: Run price comparison for all expiries.
    Equivalent to running price_option_all_expiries.py.
    
    Args:
        polymarket_csv: Path to CSV file. If None, auto-detects latest.
    """
    logger.info("-" * 80)
    logger.info("STEP 2: Running price comparison for all expiries")
    logger.info("-" * 80)
    
    try:
        if polymarket_csv:
            logger.info(f"Running price_option_all_expiries.py with CSV: {polymarket_csv}")
        else:
            logger.info("Running price_option_all_expiries.py (will auto-detect latest CSV)...")
        price_main(polymarket_quotes_csv=polymarket_csv)
        logger.info("Price comparison completed successfully")
        
    except Exception as e:
        logger.error(f"Error running price comparison: {e}")
        logger.error(traceback.format_exc())
        raise


def run_comparison(search_terms: List[str] = None, end_date: Optional[datetime] = None, polymarket_csv: Optional[str] = None):
    """
    Main function to run both steps in sequence.
    
    Args:
        search_terms: Search terms for fetching Polymarket quotes
        end_date: End date filter for fetching Polymarket quotes
        polymarket_csv: Path to CSV file (if None and no fetch, will auto-detect)
    """
    logger.info("=" * 80)
    logger.info("Starting automated comparison run")
    logger.info(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    logger.info("=" * 80)
    
    try:
        # Step 1: Fetch Polymarket quotes (if CSV not provided)
        csv_path = polymarket_csv
        
        if csv_path is None:
            # Fetch new quotes
            if search_terms is None:
                search_terms = DEFAULT_SEARCH_TERMS
            if end_date is None:
                end_date = DEFAULT_END_DATE
            
            csv_path = run_fetch_polymarket(search_terms, end_date)
            
            if csv_path is None:
                logger.warning("No CSV file generated from fetch step. Skipping price comparison.")
                logger.info("Comparison run completed (with warnings)")
                logger.info("=" * 80)
                return
        else:
            logger.info(f"Using provided CSV file: {csv_path}")
            if not os.path.exists(csv_path):
                logger.error(f"CSV file not found: {csv_path}")
                logger.info("Comparison run failed")
                logger.info("=" * 80)
                return
        
        # Step 2: Run price comparison
        run_price_comparison(polymarket_csv=csv_path)
        
        logger.info("=" * 80)
        logger.info("Comparison run completed successfully")
        logger.info("=" * 80)
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal. Shutting down gracefully...")
        raise
    except Exception as e:
        logger.error(f"Error in comparison run: {e}")
        logger.error(traceback.format_exc())
        logger.error("Continuing to next scheduled run...")


def main_loop(search_terms: List[str] = None, end_date: Optional[datetime] = None, 
              polymarket_csv: Optional[str] = None, run_once: bool = False, frequency_minutes: int = 15):
    """
    Main scheduler loop.
    Runs comparison at specified frequency (or once if run_once=True).
    
    Args:
        search_terms: Search terms for fetching Polymarket quotes
        end_date: End date filter for fetching Polymarket quotes
        polymarket_csv: Path to CSV file (if None, will fetch new quotes)
        run_once: If True, run once and exit. If False, schedule at specified frequency.
        frequency_minutes: Run frequency in minutes (15, 30, 60, etc.)
    """
    logger.info("=" * 80)
    logger.info("Starting automated scheduler")
    logger.info(f"Search terms: {search_terms if search_terms else DEFAULT_SEARCH_TERMS}")
    logger.info(f"End date filter: {end_date.strftime('%Y-%m-%d') if end_date else 'None'}")
    logger.info(f"CSV file: {polymarket_csv if polymarket_csv else 'Auto-detect/Fetch new'}")
    
    if run_once:
        logger.info(f"Mode: Run once")
    else:
        if frequency_minutes == 60:
            freq_str = "1 hour"
        else:
            freq_str = f"{frequency_minutes} minutes"
        logger.info(f"Mode: Schedule every {freq_str}")
    
    logger.info(f"Log file: {log_file}")
    logger.info("=" * 80)
    logger.info("Workflow:")
    logger.info("  1. Run fetch_polymarket_prob.py (fetch quotes and save to CSV)")
    logger.info("  2. Run price_option_all_expiries.py (compare all expiries)")
    logger.info("=" * 80)
    
    if run_once:
        # Run once and exit
        logger.info("Running single comparison...")
        run_comparison(search_terms=search_terms, end_date=end_date, polymarket_csv=polymarket_csv)
        logger.info("Single run completed.")
    else:
        # Schedule the job at specified frequency
        if frequency_minutes == 60:
            schedule.every(1).hour.do(
                lambda: run_comparison(search_terms=search_terms, end_date=end_date, polymarket_csv=polymarket_csv)
            )
        else:
            schedule.every(frequency_minutes).minutes.do(
                lambda: run_comparison(search_terms=search_terms, end_date=end_date, polymarket_csv=polymarket_csv)
            )
        
        # Run once immediately
        logger.info("Running initial comparison...")
        run_comparison(search_terms=search_terms, end_date=end_date, polymarket_csv=polymarket_csv)
        
        # Then run on schedule
        if frequency_minutes == 60:
            logger.info(f"Scheduler active. Will run every hour. Waiting for next run...")
        else:
            logger.info(f"Scheduler active. Will run every {frequency_minutes} minutes. Waiting for next run...")
        logger.info("Press Ctrl+C to stop")
        
        try:
            while True:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            logger.info("\nReceived interrupt. Shutting down...")
            logger.info("Final run completed.")


if __name__ == '__main__':
    # Parse command-line arguments
    search_terms, end_date, polymarket_csv, run_once, frequency_minutes = parse_arguments()
    
    # Run main loop with parsed arguments
    main_loop(
        search_terms=search_terms,
        end_date=end_date,
        polymarket_csv=polymarket_csv,
        run_once=run_once,
        frequency_minutes=frequency_minutes
    )

