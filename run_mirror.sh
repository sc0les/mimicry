#!/bin/bash
# Wrapper for cron — credentials loaded from secrets.env by Python, not here
export PATH="/opt/homebrew/bin:/opt/mimicry-env/bin:$PATH"

cd /opt/mimicry
/opt/mimicry-env/bin/python3 mirror.py >> /opt/mimicry/mirror.log 2>&1
