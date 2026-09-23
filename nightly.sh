#!/bin/zsh
# Run by macOS every morning. Absolute paths throughout: the scheduler
# does not load your terminal's settings, so nothing can be assumed.
set -u
cd /Users/prateeksachdeva/personal/seven_module_ai_blogpost || exit 1
export SEC_USER_AGENT="Prateek Sachdeva spreadhappinesstoall062@gmail.com"
PY=/usr/local/bin/python3

echo "=== $(date) ===" >> data/nightly.log
$PY run_nightly.py >> data/nightly.log 2>&1
$PY digest.py > data/digest-latest.txt 2>&1

# A notification disappears if you are not looking. Drop the digest
# somewhere you will actually see it.
cp data/digest-latest.txt ~/Desktop/thesis-monitor.txt

HEADLINE=$(grep -m1 -E "WARNING|CHANGED|Nothing changed|never run" data/digest-latest.txt)
osascript -e "display notification \"${HEADLINE:-see digest}\" with title \"Thesis monitor\""
