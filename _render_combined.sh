#!/usr/bin/env bash
# Combine the BTC/ETH scan outputs into one Slack-channel view.
set -e

GRAY=$'\033[90m'
WHT=$'\033[97m'
B=$'\033[1m'
IT=$'\033[3m'
YEL=$'\033[93m'
RED=$'\033[91m'
GRN=$'\033[92m'
BG_DARK=$'\033[48;5;236m'
R=$'\033[0m'

echo
echo "  ${BG_DARK}${WHT}${B}  # arb-alerts  ${R}  ${GRAY}— Predict.fun ↔ Deribit arbitrage scanner (local terminal preview, no webhook configured)${R}"
echo "  ${GRAY}══════════════════════════════════════════════════════════════════════════════════════════${R}"
echo "  ${GRAY}${IT}Test run: BTC/ETH × ≤2d expiry × 2% spread threshold${R}"

for log in /tmp/slack_test_btc.log /tmp/slack_test_eth.log; do
    # Strip the per-scan banner (lines 1-7) and the trailing summary block
    awk '
        /# arb-alerts  / { skip=1; next }
        /Settings: currencies=/ { skip=1; next }
        /Dependencies ready/ { skip=1; next }
        /Parameters set/ { skip=1; next }
        /═══/ { skip=1; next }
        /Summary:/ { skip=1; next }
        /^$/ { print; next }
        { print }
    ' "$log"
done

echo "  ${GRAY}══════════════════════════════════════════════════════════════════════════════════════════${R}"
echo "  ${B}${WHT}Channel summary:${R}  ${GRN}alerts posted${R} (BTC, ETH)"
echo
