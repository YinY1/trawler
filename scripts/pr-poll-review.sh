#!/usr/bin/env bash
# PR review polling script
# Polls Qodo bot review comments until complete or timeout.
#
# Usage: ./scripts/pr-poll-review.sh <PR_NUMBER>
#
# Process:
#   1. Poll every 3 minutes via gh pr view --comments
#   2. On detecting new Qodo comments → record count
#   3. 2 consecutive polls with no new comments → consider complete
#   4. Times out after 30 minutes

set -euo pipefail

PR_NUMBER="${1:-}"
if [ -z "$PR_NUMBER" ]; then
    echo "Usage: $0 <PR_NUMBER>"
    exit 1
fi

POLL_INTERVAL=180   # 3 minutes
MAX_POLLS=10        # 30 min / 3 min
REQUIRED_EMPTY=2    # consecutive empty polls → done
PREV_COUNT=0
CONSECUTIVE_EMPTY=0

echo "Polling PR #$PR_NUMBER for Qodo review comments..."
echo "Interval: ${POLL_INTERVAL}s, Max polls: $MAX_POLLS"
echo ""

for ((i=1; i<=MAX_POLLS; i++)); do
    sleep "$POLL_INTERVAL"

    # Count comments by qodo-code-review author
    COUNT=$(gh pr view "$PR_NUMBER" --comments --json comments 2>/dev/null \
        | python3 -c "import sys,json; comments=json.load(sys.stdin).get('comments',[]); print(sum(1 for c in comments if c['author']['login']=='qodo-code-review'))" \
        2>/dev/null || echo 0)

    if [ "$COUNT" -gt "$PREV_COUNT" ]; then
        echo "[$i/$MAX_POLLS] ⚡ New Qodo review comments! ($COUNT total)"
        PREV_COUNT=$COUNT
        CONSECUTIVE_EMPTY=0
    else
        CONSECUTIVE_EMPTY=$((CONSECUTIVE_EMPTY + 1))
        echo "[$i/$MAX_POLLS] No new comments (${CONSECUTIVE_EMPTY}/${REQUIRED_EMPTY})"

        if [ "$CONSECUTIVE_EMPTY" -ge "$REQUIRED_EMPTY" ]; then
            echo ""
            echo "✅ Review complete — 2 consecutive polls with no new comments."
            exit 0
        fi
    fi
done

echo ""
echo "⚠️  Timeout reached after 30 minutes."
echo "   Check manually: gh pr view $PR_NUMBER --comments"
exit 1
