"""
Portfolio Tracker Re-Authentication Script

Runs headlessly — designed for server/cron deployment.
Uses IMAP to read the OTP from a Gmail inbox automatically.

Trigger manually or automatically when the session is expiring.
"""

import asyncio
import imaplib
import email
import re
import time
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "secrets.env")

# ── Config ────────────────────────────────────────────────────────────────────

TRACKER_EMAIL      = os.environ["TRACKER_EMAIL"]
TRACKER_LOGIN_URL  = os.environ["TRACKER_LOGIN_URL"]
TRACKER_OTP_SENDER = os.environ["TRACKER_OTP_SENDER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
STATE_FILE         = Path(__file__).parent / "browser_state.json"

# ── Gmail IMAP OTP reader ─────────────────────────────────────────────────────

def fetch_otp_from_gmail(max_wait=90, poll_interval=4, connect_retries=5, connect_backoff=6):
    """
    Poll Gmail via IMAP for an OTP email.
    Waits up to max_wait seconds, checking every poll_interval seconds.
    Retries the initial IMAP connection up to connect_retries times with
    connect_backoff seconds between attempts (handles post-sleep network delay).
    Returns the 6-digit OTP string, or raises TimeoutError.
    """
    print(f"Polling Gmail for OTP (up to {max_wait}s)...")
    deadline = time.time() + max_wait
    mail = None

    try:
        # Retry IMAP connection — machine may have just woken from sleep
        for attempt in range(1, connect_retries + 1):
            try:
                mail = imaplib.IMAP4_SSL("imap.gmail.com")
                mail.login(TRACKER_EMAIL, GMAIL_APP_PASSWORD)
                break
            except OSError as e:
                if attempt == connect_retries:
                    raise
                print(f"  IMAP connect failed (attempt {attempt}/{connect_retries}): {e} — retrying in {connect_backoff}s...")
                time.sleep(connect_backoff)

        mail.select("inbox")

        while time.time() < deadline:
            _, msg_ids = mail.search(None, f'FROM "{TRACKER_OTP_SENDER}" UNSEEN')
            ids = msg_ids[0].split()

            for msg_id in reversed(ids):  # newest first
                _, data = mail.fetch(msg_id, "(RFC822)")
                raw = data[0][1]
                msg = email.message_from_bytes(raw)
                subject = msg.get("Subject", "")
                match = re.search(r"\b(\d{6})\b", subject)
                if match:
                    otp = match.group(1)
                    print(f"OTP found: {otp}")
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    return otp

            remaining = int(deadline - time.time())
            print(f"  No OTP yet — waiting {poll_interval}s ({remaining}s remaining)...")
            time.sleep(poll_interval)

        raise TimeoutError("OTP not received within timeout.")

    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass

# ── Playwright login flow ─────────────────────────────────────────────────────

async def run_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        print("Navigating to tracker login...")
        await page.goto(TRACKER_LOGIN_URL)
        await page.wait_for_load_state("networkidle")

        print(f"Submitting email: {TRACKER_EMAIL}")
        await page.fill('input[type="email"], input[placeholder*="email" i]', TRACKER_EMAIL)
        await page.click('button[type="submit"], button:has-text("Continue")')
        await page.wait_for_timeout(2000)

        otp = fetch_otp_from_gmail()

        print("Entering OTP...")
        otp_input = page.locator('input[placeholder="OTP"], input[type="text"], input[type="number"]').first
        await otp_input.fill(otp)
        await page.wait_for_timeout(500)
        await page.click('button:has-text("Continue with OTP"), button[type="submit"]')
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

        if "sign-in" in page.url:
            raise RuntimeError(f"Login failed — still on sign-in page. URL: {page.url}")

        print(f"Logged in. URL: {page.url}")

        await context.storage_state(path=str(STATE_FILE))
        print(f"Session saved to {STATE_FILE}")

        await browser.close()

def reauth():
    asyncio.run(run_login())

# ── Session expiry check ──────────────────────────────────────────────────────

def session_expires_in_days():
    if not STATE_FILE.exists():
        return 0
    with open(STATE_FILE) as f:
        state = json.load(f)
    for cookie in state.get("cookies", []):
        if cookie["name"] == "__Secure-better-auth.session_token":
            expires_ts = cookie.get("expires", 0)
            return max(0, (expires_ts - time.time()) / 86400)
    return 0

if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv
    days_left = session_expires_in_days()

    if force or days_left < 2:
        if not force:
            print(f"Session expires in {days_left:.1f} days — re-authenticating...")
        else:
            print("Forcing re-authentication...")
        reauth()
        print("Re-authentication complete.")
    else:
        print(f"Session is healthy ({days_left:.1f} days remaining). Use --force to re-auth anyway.")
