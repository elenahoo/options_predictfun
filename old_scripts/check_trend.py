#!/usr/bin/env python3
"""
Script to analyze trends in flagged opportunities by reading all CSV files
from the flagged directory and plotting Spread_pct and CI_margin_pct over time.
"""

import os
import glob
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import numpy as np
from pathlib import Path

def extract_timestamp_from_filename(filename):
    """
    Extract the last 6 characters before .csv as the timestamp.
    Example: flagged_opportunities_summary_20260104_184937.csv -> 184937
    """
    base_name = os.path.basename(filename)
    # Remove .csv extension
    name_without_ext = base_name.replace('.csv', '')
    # Get last 6 characters
    timestamp = name_without_ext[-6:]
    return timestamp

def read_all_flagged_files(flagged_dir):
    """
    Read all CSV files from the flagged directory and collect data.
    Returns a list of dictionaries with timestamp, Spread_pct, CI_margin_pct, and group_key.
    """
    all_data = []
    
    # Get all CSV files in the flagged directory
    csv_files = glob.glob(os.path.join(flagged_dir, '*.csv'))
    
    print(f"Found {len(csv_files)} CSV files")
    
    for csv_file in csv_files:
        try:
            # Extract timestamp from filename
            timestamp = extract_timestamp_from_filename(csv_file)
            
            # Read the CSV file
            df = pd.read_csv(csv_file)
            
            # Check if required columns exist
            required_cols = ['Spread_pct', 'CI_margin_pct', 'expiry_date', 'K']
            if all(col in df.columns for col in required_cols):
                # Collect all rows with their timestamps and group keys
                for _, row in df.iterrows():
                    # Create group key from expiry_date and K
                    group_key = f"{row['expiry_date']}+{row['K']}"
                    all_data.append({
                        'timestamp': timestamp,
                        'Spread_pct': row['Spread_pct'],
                        'CI_margin_pct': row['CI_margin_pct'],
                        'group_key': group_key,
                        'expiry_date': row['expiry_date'],
                        'K': row['K']
                    })
            else:
                print(f"Warning: {csv_file} missing required columns")
                
        except Exception as e:
            print(f"Error reading {csv_file}: {e}")
    
    return all_data

def plot_trends(data, output_dir=None):
    """
    Plot Spread_pct and CI_margin_pct against timestamp in ascending order,
    with different colors for each expiry_date+K combination using interactive Plotly line charts.
    """
    if not data:
        print("No data to plot")
        return
    
    # Convert to DataFrame for easier manipulation
    df = pd.DataFrame(data)
    
    # Convert timestamp to integer for sorting (HHMMSS format)
    df['timestamp_int'] = df['timestamp'].astype(int)
    
    # Sort by timestamp in ascending order
    df_sorted = df.sort_values('timestamp_int')
    
    # Get unique group keys
    unique_groups = df_sorted['group_key'].unique()
    n_groups = len(unique_groups)
    
    # Use Plotly's default color palette
    import plotly.colors as pc
    # Get a color sequence - use qualitative colors
    # Plotly's default palette has 10 colors, we'll cycle through them
    default_colors = pc.qualitative.Plotly
    # Extend with additional palettes if needed
    extended_colors = default_colors + pc.qualitative.Set3[:10] + pc.qualitative.Dark2[:8]
    
    # Create a mapping from group_key to color
    group_color_map = {group: extended_colors[i % len(extended_colors)] 
                      for i, group in enumerate(unique_groups)}
    
    # Create subplots with shared x-axis
    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=('Spread_pct Trend Over Time (Grouped by expiry_date+K)',
                       'CI_margin_pct Trend Over Time (Grouped by expiry_date+K)'),
        vertical_spacing=0.1,
        shared_xaxes=True
    )
    
    # Plot 1: Spread_pct vs timestamp (grouped by expiry_date+K) - Line chart
    for group_key in unique_groups:
        group_data = df_sorted[df_sorted['group_key'] == group_key].sort_values('timestamp_int')
        fig.add_trace(
            go.Scatter(
                x=group_data['timestamp_int'],
                y=group_data['Spread_pct'],
                mode='lines+markers',
                name=group_key,
                line=dict(color=group_color_map[group_key], width=2),
                marker=dict(size=6),
                hovertemplate=f'<b>{group_key}</b><br>' +
                             'Timestamp: %{x}<br>' +
                             'Spread_pct: %{y:.2f}%<br>' +
                             '<extra></extra>',
                legendgroup=group_key,
                showlegend=True
            ),
            row=1, col=1
        )
    
    # Plot 2: CI_margin_pct vs timestamp (grouped by expiry_date+K) - Line chart
    for group_key in unique_groups:
        group_data = df_sorted[df_sorted['group_key'] == group_key].sort_values('timestamp_int')
        fig.add_trace(
            go.Scatter(
                x=group_data['timestamp_int'],
                y=group_data['CI_margin_pct'],
                mode='lines+markers',
                name=group_key,
                line=dict(color=group_color_map[group_key], width=2),
                marker=dict(size=6),
                hovertemplate=f'<b>{group_key}</b><br>' +
                             'Timestamp: %{x}<br>' +
                             'CI_margin_pct: %{y:.2f}%<br>' +
                             '<extra></extra>',
                legendgroup=group_key,
                showlegend=False  # Only show legend once
            ),
            row=2, col=1
        )
    
    # Update x-axis labels
    fig.update_xaxes(title_text="Timestamp (HHMMSS)", row=2, col=1)
    
    # Update y-axis labels
    fig.update_yaxes(title_text="Spread_pct (%)", row=1, col=1)
    fig.update_yaxes(title_text="CI_margin_pct (%)", row=2, col=1)
    
    # Update layout
    fig.update_layout(
        height=800,
        title_text="Trend Analysis Over Time",
        title_x=0.5,
        hovermode='closest',
        legend=dict(
            orientation="v",
            yanchor="top",
            y=1,
            xanchor="left",
            x=1.02
        )
    )
    
    # Save the plot as HTML (interactive)
    if output_dir:
        output_path = os.path.join(output_dir, 'trend_analysis.html')
    else:
        output_path = 'trend_analysis.html'
    
    fig.write_html(output_path)
    print(f"Interactive plot saved to {output_path}")
    
    # Also show the plot in browser
    fig.show()
    
    # Print summary statistics
    print("\nSummary Statistics:")
    print(f"Total data points: {len(df_sorted)}")
    print(f"Number of unique groups (expiry_date+K): {n_groups}")
    print(f"\nSpread_pct:")
    print(f"  Mean: {df_sorted['Spread_pct'].mean():.2f}%")
    print(f"  Median: {df_sorted['Spread_pct'].median():.2f}%")
    print(f"  Min: {df_sorted['Spread_pct'].min():.2f}%")
    print(f"  Max: {df_sorted['Spread_pct'].max():.2f}%")
    print(f"\nCI_margin_pct:")
    print(f"  Mean: {df_sorted['CI_margin_pct'].mean():.2f}%")
    print(f"  Median: {df_sorted['CI_margin_pct'].median():.2f}%")
    print(f"  Min: {df_sorted['CI_margin_pct'].min():.2f}%")
    print(f"  Max: {df_sorted['CI_margin_pct'].max():.2f}%")
    
    # Print group information
    print(f"\nGroups (expiry_date+K):")
    for group_key in sorted(unique_groups):
        group_data = df_sorted[df_sorted['group_key'] == group_key]
        print(f"  {group_key}: {len(group_data)} data points")

def main():
    """Main function to run the trend analysis."""
    # Get the script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Path to flagged directory
    flagged_dir = os.path.join(script_dir, 'flagged')
    
    if not os.path.exists(flagged_dir):
        print(f"Error: Flagged directory not found at {flagged_dir}")
        return
    
    print(f"Reading CSV files from: {flagged_dir}")
    
    # Read all flagged files
    data = read_all_flagged_files(flagged_dir)
    
    if not data:
        print("No data found in CSV files")
        return
    
    print(f"Collected {len(data)} data points")
    
    # Plot the trends
    plot_trends(data, output_dir=script_dir)

if __name__ == '__main__':
    main()

