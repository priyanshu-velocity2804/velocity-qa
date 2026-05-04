#!/bin/zsh
# Daily QA analysis — runs automatically at 7pm via cron
# Analyzes 50 Order Confirmation + 50 NDR calls and writes to Google Sheets

SCRIPT_DIR="/Users/priyanshugupta/Claude Design"
LOG_DIR="$SCRIPT_DIR/logs"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/qa_$DATE.log"

mkdir -p "$LOG_DIR"

echo "======================================" >> "$LOG_FILE"
echo "  Velocity QA — Daily Run" >> "$LOG_FILE"
echo "  Started: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
echo "======================================" >> "$LOG_FILE"

cd "$SCRIPT_DIR"

echo "" >> "$LOG_FILE"
echo "--- ORDER CONFIRMATION (50 calls) ---" >> "$LOG_FILE"
/usr/bin/python3 "$SCRIPT_DIR/analyze_calls.py" --call-type oc --limit 50 >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
echo "--- NDR (50 calls) ---" >> "$LOG_FILE"
/usr/bin/python3 "$SCRIPT_DIR/analyze_calls.py" --call-type ndr --limit 50 >> "$LOG_FILE" 2>&1

echo "" >> "$LOG_FILE"
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"

# Keep only last 30 days of logs
find "$LOG_DIR" -name "qa_*.log" -mtime +30 -delete
