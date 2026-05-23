"""
Arbitrage Detection System for Polymarket Range Bets vs Deribit Options

This system:
1. Identifies Polymarket range bets (e.g., BTC between 92000-94000)
2. Finds equivalent Deribit option spreads to replicate the bet
3. Calculates costs and potential arbitrage opportunities
4. Determines optimal trade sizes for hedging/arbitrage
"""

import sqlite3
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime
import re

@dataclass
class RangeBet:
    """Represents a Polymarket range bet"""
    lower_strike: float
    upper_strike: float
    expiry_date: str
    polymarket_cost: float  # Cost to buy the bet (in $)
    polymarket_payout: float  # Payout if bet wins (typically $100)
    probability: float  # Implied probability from market
    slug: str
    url: str

@dataclass
class OptionSpread:
    """Represents a call spread on Deribit"""
    lower_strike: float
    upper_strike: float
    expiry_date: str
    lower_call_bid: float  # Bid price for lower strike call
    lower_call_ask: float  # Ask price for lower strike call
    upper_call_bid: float  # Bid price for upper strike call
    upper_call_ask: float  # Ask price for upper strike call
    spread_cost: float  # Cost to buy the spread (ask - ask)
    spread_value: float  # Value if selling the spread (bid - bid)

@dataclass
class ArbitrageOpportunity:
    """Represents a potential arbitrage opportunity"""
    range_bet: RangeBet
    option_spread: OptionSpread
    polymarket_cost_per_unit: float  # Cost per $1 payout
    deribit_cost_per_unit: float  # Cost per $1 payout
    profit_per_unit: float  # Profit if arbitrage works
    optimal_polymarket_size: float  # Optimal bet size on Polymarket
    optimal_deribit_size: float  # Optimal spread size on Deribit
    total_profit: float  # Total profit if executed
    risk_free: bool  # Whether this is risk-free arbitrage

class ArbitrageAnalyzer:
    def __init__(self, db_path: str = 'deribit_data.db'):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        
    def close(self):
        self.conn.close()
    
    def extract_range_from_slug(self, slug: str, strike_price: float) -> Optional[Tuple[float, float]]:
        """
        Extract range bounds from Polymarket slug.
        Examples:
        - "will-the-price-of-bitcoin-be-between-92000-94000-on-january-7" -> (92000, 94000)
        - "bitcoin-above-98k-on-january-7" -> (98000, inf) - single strike
        """
        # Pattern for range bets: "between-X-Y"
        range_pattern = r'between[_-](\d+(?:\.\d+)?)[_-](\d+(?:\.\d+)?)'
        match = re.search(range_pattern, slug, re.IGNORECASE)
        if match:
            lower = float(match.group(1))
            upper = float(match.group(2))
            return (lower, upper)
        
        # Pattern for "above X" bets - these are single strike, not ranges
        # We'll handle these separately
        above_pattern = r'above[_-](\d+(?:\.\d+)?)'
        match = re.search(above_pattern, slug, re.IGNORECASE)
        if match:
            strike = float(match.group(1))
            return (strike, float('inf'))  # Upper bound is infinity
        
        # If no pattern matches, assume it's a single strike bet
        return (strike_price, float('inf'))
    
    def get_polymarket_range_bets(self, expiry_date: Optional[str] = None) -> List[RangeBet]:
        """
        Extract range bets from Polymarket quotes.
        Returns list of RangeBet objects.
        """
        query = """
            SELECT strike_price_K, probability_p, bestAsk, bestBid, 
                   lastTradePrice, expiry_date, slug, polymarket_url
            FROM polymarket_quotes
        """
        params = []
        if expiry_date:
            query += " WHERE expiry_date = ?"
            params.append(expiry_date)
        
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        range_bets = []
        for row in rows:
            strike = row['strike_price_K']
            slug = row['slug']
            expiry = row['expiry_date']
            
            # Extract range from slug
            range_bounds = self.extract_range_from_slug(slug, strike)
            if not range_bounds:
                continue
            
            lower, upper = range_bounds
            
            # Skip single strike "above" bets for now (focus on ranges)
            if upper == float('inf'):
                continue
            
            # Calculate cost - use bestAsk as the cost to buy
            best_ask = row['bestAsk']
            best_bid = row['bestBid']
            last_trade = row['lastTradePrice']
            
            # Use bestAsk if available, otherwise lastTradePrice, otherwise bestBid
            # Handle empty strings and None values
            cost = None
            if best_ask and str(best_ask).strip() and str(best_ask) != '':
                try:
                    cost = float(best_ask)
                except (ValueError, TypeError):
                    pass
            if cost is None and last_trade and str(last_trade).strip() and str(last_trade) != '':
                try:
                    cost = float(last_trade)
                except (ValueError, TypeError):
                    pass
            if cost is None and best_bid and str(best_bid).strip() and str(best_bid) != '':
                try:
                    cost = float(best_bid)
                except (ValueError, TypeError):
                    pass
            
            if cost is None:
                continue
            
            # Polymarket bets typically pay $1 per share, so cost is the probability
            # But we need to normalize: if cost is 0.24, that means $24 for $100 payout
            payout = 100.0  # Standard Polymarket payout
            cost_per_unit = float(cost)
            
            range_bet = RangeBet(
                lower_strike=lower,
                upper_strike=upper,
                expiry_date=expiry,
                polymarket_cost=cost_per_unit * payout,  # Total cost for full payout
                polymarket_payout=payout,
                probability=row['probability_p'],
                slug=slug,
                url=row['polymarket_url']
            )
            range_bets.append(range_bet)
        
        return range_bets
    
    def get_deribit_options_for_expiry(self, expiry_date: str) -> pd.DataFrame:
        """
        Get all Deribit call options for a specific expiry date.
        Returns DataFrame with columns: Strike, Bid_Price, Ask_Price
        """
        query = """
            SELECT strike, 
                   AVG(CASE WHEN bid_price IS NOT NULL THEN bid_price END) as bid_price,
                   AVG(CASE WHEN ask_price IS NOT NULL THEN ask_price END) as ask_price
            FROM deribit_prices
            WHERE expiry = ?
            GROUP BY strike
            ORDER BY strike
        """
        
        df = pd.read_sql_query(query, self.conn, params=(expiry_date,))
        return df
    
    def find_matching_spread(self, range_bet: RangeBet, deribit_options: pd.DataFrame) -> Optional[OptionSpread]:
        """
        Find the Deribit call spread that matches a Polymarket range bet.
        
        A range bet [K1, K2] can be replicated as:
        - Long call(K1) - Long call(K2) = Bull call spread
        
        This pays $1 per unit if price ends up between K1 and K2.
        """
        # Find options at lower and upper strikes
        lower_options = deribit_options[deribit_options['strike'] == range_bet.lower_strike]
        upper_options = deribit_options[deribit_options['strike'] == range_bet.upper_strike]
        
        if lower_options.empty or upper_options.empty:
            return None
        
        lower_row = lower_options.iloc[0]
        upper_row = upper_options.iloc[0]
        
        lower_bid = lower_row['bid_price']
        lower_ask = lower_row['ask_price']
        upper_bid = upper_row['bid_price']
        upper_ask = upper_row['ask_price']
        
        # Check if we have valid prices
        if pd.isna(lower_ask) or pd.isna(upper_ask):
            return None
        
        # Cost to buy the spread: Buy lower call, sell upper call
        # Actually, for a range bet, we want: Long call(K1) - Long call(K2)
        # This means: Buy call(K1) at ask, Sell call(K2) at bid
        # Net cost = lower_ask - upper_bid
        
        # Cost to BUY the spread: Buy lower call at ask, Sell upper call at bid
        spread_cost = lower_ask - (upper_bid if not pd.isna(upper_bid) else 0)
        
        # Value when SELLING the spread: Sell lower call at bid, Buy upper call at ask  
        # This is what we RECEIVE (can be negative if we pay more than we receive)
        spread_value = (lower_bid if not pd.isna(lower_bid) else 0) - (upper_ask if not pd.isna(upper_ask) else 0)
        
        return OptionSpread(
            lower_strike=range_bet.lower_strike,
            upper_strike=range_bet.upper_strike,
            expiry_date=range_bet.expiry_date,
            lower_call_bid=lower_bid,
            lower_call_ask=lower_ask,
            upper_call_bid=upper_bid,
            upper_call_ask=upper_ask,
            spread_cost=spread_cost,
            spread_value=spread_value
        )
    
    def calculate_arbitrage(self, range_bet: RangeBet, option_spread: OptionSpread) -> Optional[ArbitrageOpportunity]:
        """
        Calculate arbitrage opportunity between Polymarket bet and Deribit spread.
        
        Strategy:
        - Buy Polymarket bet at cost C_p
        - Replicate on Deribit: Buy call(K1) at ask, Sell call(K2) at bid
        - Net Deribit cost = lower_ask - upper_bid
        
        If C_p < (lower_ask - upper_bid), we can:
        - Buy Polymarket bet
        - Sell the spread on Deribit (sell call(K1), buy call(K2))
        - Profit = (lower_bid - upper_ask) - C_p
        
        If C_p > (lower_ask - upper_bid), we can:
        - Sell Polymarket bet (if possible) or skip
        - Buy the spread on Deribit
        - This is not risk-free arbitrage, just a price difference
        """
        # Normalize costs per $1 payout
        polymarket_cost_per_unit = range_bet.polymarket_cost / range_bet.polymarket_payout
        
        # Deribit spread: to replicate $1 payout, we need spread width = upper - lower
        # But Deribit options are priced per BTC, not per $1
        # We need to convert: if spread is (94000 - 92000) = 2000, and option costs are in USD
        # Actually, Deribit USDC options settle in USD, so prices are already in USD
        
        # The spread pays (upper - lower) if price is above upper, and (price - lower) if between
        # For a range bet that pays $1 if in range, we need to normalize
        
        # Actually, let's think differently:
        # Polymarket bet pays $100 if price in [92000, 94000]
        # Deribit spread: Long call(92000) - Long call(94000)
        # This spread pays: max(0, S-92000) - max(0, S-94000)
        # = 0 if S < 92000
        # = S - 92000 if 92000 <= S < 94000  
        # = 2000 if S >= 94000
        
        # To get $100 payout, we need: (spread_payout / 2000) * 100 = $100
        # So we need: spread_payout = 2000
        # Which means we need: 2000 / (upper - lower) = 1 unit of spread
        
        spread_width = option_spread.upper_strike - option_spread.lower_strike
        
        if spread_width == 0:
            return None
        
        # Cost to replicate $1 payout on Deribit
        # We need (1 / spread_width) units of the spread
        units_needed = 1.0 / spread_width
        
        # Calculate costs for both buying and selling the spread
        # Buying spread: lower_ask - upper_bid (cost to buy)
        # Selling spread: lower_bid - upper_ask (what we receive when selling)
        deribit_cost_buy = option_spread.spread_cost * units_needed  # Cost to buy spread
        deribit_cost_sell = abs(option_spread.spread_value) * units_needed  # What we get selling spread
        
        # Compare Polymarket cost vs Deribit costs
        # If we can sell Deribit spread for more than Polymarket costs, arbitrage!
        profit_selling_deribit = deribit_cost_sell - polymarket_cost_per_unit
        
        # If we can buy Deribit spread for less than Polymarket costs, reverse arbitrage
        profit_buying_deribit = polymarket_cost_per_unit - deribit_cost_buy
        
        # Determine optimal sizes
        trade_size = 1000.0
        
        if profit_selling_deribit > 0:
            # Arbitrage: We can sell Deribit spread for more than Polymarket costs
            # Strategy: Buy Polymarket bet (cheaper), Sell Deribit spread (receive more)
            # Selling spread = Sell lower strike call at bid, Buy upper strike call at ask
            
            optimal_polymarket_size = trade_size / polymarket_cost_per_unit
            optimal_deribit_size = optimal_polymarket_size * units_needed
            total_profit = profit_selling_deribit * optimal_polymarket_size
            
            return ArbitrageOpportunity(
                range_bet=range_bet,
                option_spread=option_spread,
                polymarket_cost_per_unit=polymarket_cost_per_unit,
                deribit_cost_per_unit=deribit_cost_sell,  # What we receive selling
                profit_per_unit=profit_selling_deribit,
                optimal_polymarket_size=optimal_polymarket_size,
                optimal_deribit_size=optimal_deribit_size,
                total_profit=total_profit,
                risk_free=True  # This is risk-free if we can execute both sides
            )
        elif profit_buying_deribit > 0:
            # Reverse arbitrage: We can buy Deribit spread for less than Polymarket costs
            # Strategy: Sell Polymarket bet (if possible), Buy Deribit spread (cheaper)
            # This is harder to execute since we can't easily sell Polymarket bets
            
            optimal_polymarket_size = trade_size / polymarket_cost_per_unit
            optimal_deribit_size = optimal_polymarket_size * units_needed
            total_profit = profit_buying_deribit * optimal_polymarket_size
            
            return ArbitrageOpportunity(
                range_bet=range_bet,
                option_spread=option_spread,
                polymarket_cost_per_unit=polymarket_cost_per_unit,
                deribit_cost_per_unit=deribit_cost_buy,  # Cost to buy
                profit_per_unit=profit_buying_deribit,
                optimal_polymarket_size=optimal_polymarket_size,
                optimal_deribit_size=optimal_deribit_size,
                total_profit=total_profit,
                risk_free=False  # Not easily executable since we can't sell Polymarket bets
            )
        
        return None
    
    def find_all_arbitrage_opportunities(self, expiry_date: Optional[str] = None, min_profit: float = 0.01) -> List[ArbitrageOpportunity]:
        """
        Find all arbitrage opportunities across all range bets.
        
        Args:
            expiry_date: Filter by expiry date (optional)
            min_profit: Minimum profit per unit to consider (default: $0.01)
        
        Returns:
            List of ArbitrageOpportunity objects
        """
        opportunities = []
        
        # Get all range bets
        range_bets = self.get_polymarket_range_bets(expiry_date)
        print(f"Found {len(range_bets)} Polymarket range bets")
        
        # Group by expiry date
        by_expiry = {}
        for bet in range_bets:
            if bet.expiry_date not in by_expiry:
                by_expiry[bet.expiry_date] = []
            by_expiry[bet.expiry_date].append(bet)
        
        # Process each expiry
        for expiry, bets in by_expiry.items():
            print(f"\nProcessing expiry: {expiry}")
            
            # Get Deribit options for this expiry
            deribit_options = self.get_deribit_options_for_expiry(expiry)
            if deribit_options.empty:
                print(f"  No Deribit options found for {expiry}")
                continue
            
            print(f"  Found {len(deribit_options)} Deribit strikes")
            
            # Find matching spreads for each range bet
            for bet in bets:
                spread = self.find_matching_spread(bet, deribit_options)
                if not spread:
                    print(f"    No matching spread found for range ${bet.lower_strike:,.0f}-${bet.upper_strike:,.0f}")
                    continue
                
                arb = self.calculate_arbitrage(bet, spread)
                if arb:
                    if arb.profit_per_unit >= min_profit:
                        opportunities.append(arb)
                        print(f"    ✓ Found opportunity: profit ${arb.profit_per_unit:.4f} per $1")
                    else:
                        print(f"    Opportunity found but profit ${arb.profit_per_unit:.6f} below threshold ${min_profit}")
                else:
                    print(f"    No arbitrage opportunity for range ${bet.lower_strike:,.0f}-${bet.upper_strike:,.0f} (costs are equal)")
        
        return opportunities
    
    def print_opportunities(self, opportunities: List[ArbitrageOpportunity]):
        """Print arbitrage opportunities in a readable format"""
        if not opportunities:
            print("\nNo arbitrage opportunities found.")
            return
        
        print(f"\n{'='*100}")
        print(f"FOUND {len(opportunities)} ARBITRAGE OPPORTUNITIES")
        print(f"{'='*100}\n")
        
        for i, arb in enumerate(opportunities, 1):
            print(f"Opportunity #{i}")
            print(f"  Polymarket Bet: BTC between ${arb.range_bet.lower_strike:,.0f} - ${arb.range_bet.upper_strike:,.0f}")
            print(f"  Expiry: {arb.range_bet.expiry_date}")
            print(f"  Polymarket URL: {arb.range_bet.url}")
            print(f"\n  Costs:")
            print(f"    Polymarket: ${arb.polymarket_cost_per_unit:.4f} per $1 payout")
            print(f"    Deribit:    ${arb.deribit_cost_per_unit:.4f} per $1 payout")
            print(f"    Profit:      ${arb.profit_per_unit:.4f} per $1 payout")
            print(f"\n  Deribit Spread:")
            print(f"    Buy call(${arb.option_spread.lower_strike:,.0f}) at ask: ${arb.option_spread.lower_call_ask:,.2f}")
            print(f"    Sell call(${arb.option_spread.upper_strike:,.0f}) at bid: ${arb.option_spread.upper_call_bid:,.2f}")
            print(f"    Net cost: ${arb.option_spread.spread_cost:,.2f}")
            print(f"\n  Optimal Trade Sizes:")
            print(f"    Polymarket bet size: ${arb.optimal_polymarket_size:,.2f}")
            print(f"    Deribit spread size: {arb.optimal_deribit_size:.4f} units")
            print(f"    Total profit: ${arb.total_profit:,.2f}")
            print(f"\n  Strategy:")
            if arb.risk_free:
                print(f"    Arbitrage: Polymarket is CHEAPER than Deribit")
                print(f"    1. Buy ${arb.optimal_polymarket_size:,.2f} worth of Polymarket bet")
                print(f"    2. Sell {arb.optimal_deribit_size:.4f} units of call spread on Deribit (replicate the bet)")
                print(f"       - Sell {arb.optimal_deribit_size:.4f} call(${arb.option_spread.lower_strike:,.0f}) at bid: ${arb.option_spread.lower_call_bid:,.2f}")
                print(f"       - Buy {arb.optimal_deribit_size:.4f} call(${arb.option_spread.upper_strike:,.0f}) at ask: ${arb.option_spread.upper_call_ask:,.2f}")
                print(f"    3. Net profit: ${arb.total_profit:,.2f} (risk-free if both sides execute)")
            else:
                print(f"    Note: Polymarket is MORE EXPENSIVE than Deribit")
                print(f"    This opportunity requires selling Polymarket bets, which may not be easily executable")
                print(f"    1. Sell ${arb.optimal_polymarket_size:,.2f} worth of Polymarket bet (if possible)")
                print(f"    2. Buy {arb.optimal_deribit_size:.4f} units of call spread on Deribit")
                print(f"       - Buy {arb.optimal_deribit_size:.4f} call(${arb.option_spread.lower_strike:,.0f}) at ask: ${arb.option_spread.lower_call_ask:,.2f}")
                print(f"       - Sell {arb.optimal_deribit_size:.4f} call(${arb.option_spread.upper_strike:,.0f}) at bid: ${arb.option_spread.upper_call_bid:,.2f}")
                print(f"    3. Potential profit: ${arb.total_profit:,.2f} (if Polymarket bet can be sold)")
            print(f"\n{'='*100}\n")


def main():
    """Example usage"""
    analyzer = ArbitrageAnalyzer()
    
    try:
        # Find all arbitrage opportunities
        print("="*100)
        print("ARBITRAGE ANALYSIS")
        print("="*100)
        opportunities = analyzer.find_all_arbitrage_opportunities(min_profit=0.001)  # Lower threshold for testing
        
        # Print results
        analyzer.print_opportunities(opportunities)
        
        # Example: Solve the specific problem from the user
        print("\n" + "="*100)
        print("SOLVING USER'S SPECIFIC EXAMPLE")
        print("="*100)
        print("\nPolymarket Trade:")
        print("  Bought $24 worth of Yes bet on BTC between 92000-94000 on January 6, 2026")
        print("  Payout: $100 if price in range, profit: $76")
        print("\nDeribit Options:")
        print("  Call 91000: bid=1100, ask=1180")
        print("  Call 92000: bid=655, ask=700")
        print("  Call 93000: bid=355, ask=395")
        print("  Call 94000: bid=170, ask=210")
        
        # Create a manual range bet for this example
        example_bet = RangeBet(
            lower_strike=92000.0,
            upper_strike=94000.0,
            expiry_date="2026-01-06",
            polymarket_cost=24.0,
            polymarket_payout=100.0,
            probability=0.24,
            slug="example",
            url="example"
        )
        
        # Create manual option spread
        example_spread = OptionSpread(
            lower_strike=92000.0,
            upper_strike=94000.0,
            expiry_date="2026-01-06",
            lower_call_bid=655.0,
            lower_call_ask=700.0,
            upper_call_bid=170.0,
            upper_call_ask=210.0,
            spread_cost=700.0 - 170.0,  # Buy lower at ask, sell upper at bid
            spread_value=655.0 - 210.0   # Sell lower at bid, buy upper at ask
        )
        
        example_arb = analyzer.calculate_arbitrage(example_bet, example_spread)
        if example_arb:
            analyzer.print_opportunities([example_arb])
        else:
            print("\nNo arbitrage opportunity found for this example.")
            print("This might mean Deribit is more expensive, or calculation needs adjustment.")
            print("\nLet's check the costs manually:")
            polymarket_cost_per_unit = example_bet.polymarket_cost / example_bet.polymarket_payout
            spread_width = example_spread.upper_strike - example_spread.lower_strike
            units_needed = 1.0 / spread_width
            deribit_cost_per_unit = example_spread.spread_cost * units_needed
            print(f"  Polymarket cost per $1: ${polymarket_cost_per_unit:.4f}")
            print(f"  Deribit cost per $1: ${deribit_cost_per_unit:.4f}")
            print(f"  Difference: ${deribit_cost_per_unit - polymarket_cost_per_unit:.4f}")
        
    finally:
        analyzer.close()


if __name__ == '__main__':
    main()