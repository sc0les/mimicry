#!/bin/bash
# Wrapper for cron — credentials loaded from secrets.env by Python, not here
export PATH="/opt/homebrew/bin:/Users/samuelcolelli/.pyenv/versions/3.10.12/bin:$PATH"

cd /Users/samuelcolelli/citrindex
/Users/samuelcolelli/.pyenv/versions/3.10.12/bin/python3 mirror.py >> /Users/samuelcolelli/citrindex/mirror.log 2>&1
