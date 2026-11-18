import os
import asyncio
import time
import random
import re
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import httpx
from dotenv import load_dotenv


load_dotenv()

# CONFIGURATION
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TARGET_URL = os.getenv("TARGET_URL")
RUN_ONCE = os.getenv("RUN_ONCE", "false").lower() in ("true", "1", "yes")

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("Missing Telegram credentials! Please ensure BOT_TOKEN and CHAT_ID are set in .env or environment variables.")

if not TARGET_URL:
    raise ValueError("Missing TARGET_URL! Please set TARGET_URL in .env or environment variables.")


MOVIE_SLUG = TARGET_URL.strip("/").split("/")[-1]
MOVIE_NAME = MOVIE_SLUG.replace("-", " ").title()

class RateLimitedException(Exception):
    """Raised when Star Cineplex or Cloudflare issues a 403, 429, or CAPTCHA/WAF challenge."""
    pass

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        httpx.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=10.0)
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Telegram error: {e}")

async def check_cineplex_dom(page):
    response = await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)

    # Anti-Bot / Rate-Limit checks
    if response and response.status in (403, 429, 503):
        raise RateLimitedException(f"HTTP {response.status} returned by server")

    page_title = (await page.title()).lower()
    waf_signatures = ["access denied", "just a moment", "security check", "cloudflare", "attention required", "challenge-running"]
    if any(sig in page_title for sig in waf_signatures):
        raise RateLimitedException(f"Security challenge page detected in title: '{page_title}'")

    # Allow client-side framework to hydrate components
    await asyncio.sleep(2) 
    
    html = await page.content()
    soup = BeautifulSoup(html, "html.parser")
    
    reasons = []

    # 1. PRIMARY TARGET: #showtimes Section 
    showtimes_sec = soup.find("section", id="showtimes")
    
    if not showtimes_sec:
        reasons.append("The entire <section id='showtimes'> vanished or was renamed!")
    else:
        placeholder_text = showtimes_sec.find(string=re.compile(r"Bookings open closer to release", re.IGNORECASE))
        if not placeholder_text:
            reasons.append("Placeholder text 'Bookings open closer to release' is GONE!")
            
        if showtimes_sec.find("div", attrs={"aria-live": "polite"}):
            reasons.append("Active branch container (aria-live='polite') detected!")
            
        stubs = showtimes_sec.find_all("button", class_=re.compile(r"stub"))
        if stubs:
            reasons.append(f"Found {len(stubs)} clickable showtime buttons!")

    # 2. SECONDARY TARGET: Top Hero Badge
    eyebrow = soup.find("span", class_=re.compile(r"editorial-eyebrow"))
    if eyebrow:
        eyebrow_text = eyebrow.get_text(strip=True).lower()
        if "coming soon" not in eyebrow_text:
            reasons.append(f"Top badge changed to: '{eyebrow.get_text(strip=True)}'")

    if reasons:
        return True, "\n• ".join(reasons)

    return False, None

async def main():
    print(f"🚀 Initializing Cineplex Monitor for: {MOVIE_NAME}...")
    if RUN_ONCE:
        print("ℹ️ Running in single-check mode (RUN_ONCE=true).")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        print(f"🟢 Connected. Monitoring DOM for {MOVIE_NAME}...")

        check_count = 0
        consecutive_errors = 0

        while True:
            try:
                check_count += 1
                is_live, reason = await check_cineplex_dom(page)
                consecutive_errors = 0  # Reset error streak on success

                if is_live:
                    print(f"🚨 {MOVIE_NAME.upper()} TICKETS LIVE! Detected:\n• {reason}")
                    
                    for i in range(1, 11):
                        send_telegram(
                            f"🚨 [ALERT {i}/10] {MOVIE_NAME.upper()} TICKETS DETECTED! 🚨\n\n"
                            f"Changes:\n• {reason}\n\n"
                            f"👉 Book Now: {TARGET_URL}"
                        )
                        await asyncio.sleep(3) 
                    
                    await browser.close()
                    return

                if RUN_ONCE:
                    print(f"[{time.strftime('%H:%M:%S')}] Check finished (RUN_ONCE). No changes. Exiting.")
                    await browser.close()
                    return

                # SAFETY NET: HUMAN BREAKS & CADENCE RANDOMIZATION 
                # Every 6 to 10 checks, pause for 5 to 8 minutes to break automated rhythm
                if check_count % random.randint(6, 10) == 0:
                    break_sec = random.randint(300, 480)
                    print(f"[{time.strftime('%H:%M:%S')}] ☕ Taking a human safety break ({break_sec // 60}m) to avoid bot detection...")
                    await asyncio.sleep(break_sec)
                else:
                    sleep_sec = random.randint(120, 240)
                    print(f"[{time.strftime('%H:%M:%S')}] No changes for {MOVIE_NAME}. Placeholder intact. Next check in {sleep_sec}s...")
                    await asyncio.sleep(sleep_sec)

            except RateLimitedException as e:
                # Generous backoff for bot bans or rate limits: 15 to 25 minutes
                cooldown_sec = random.randint(900, 1500)
                print(f"[{time.strftime('%H:%M:%S')}] 🛡️ [ANTI-BAN TRIGGERED] {e}")
                print(f"[{time.strftime('%H:%M:%S')}] Pausing monitor for {cooldown_sec // 60} minutes to allow IP cooldown...")
                send_telegram(
                    f"⚠️ [MONITOR PAUSED] Star Cineplex / Cloudflare rate limit detected ({e}).\n"
                    f"Entering {cooldown_sec // 60}-minute cooldown to prevent IP ban."
                )
                await asyncio.sleep(cooldown_sec)

                # Reset page context to clear flagged cookies / session tokens
                try:
                    await page.close()
                    page = await context.new_page()
                except Exception as reset_err:
                    print(f"[{time.strftime('%H:%M:%S')}] Page reset failed: {reset_err}")

            except Exception as e:
                consecutive_errors += 1
                print(f"[{time.strftime('%H:%M:%S')}] Browser/Network error (streak {consecutive_errors}): {e}")

                if RUN_ONCE:
                    print("Exiting due to error in RUN_ONCE mode.")
                    await browser.close()
                    return

                # Exponential backoff for network/browser glitches
                if consecutive_errors == 1:
                    backoff_sec = 60
                elif consecutive_errors == 2:
                    backoff_sec = 180
                else:
                    backoff_sec = random.randint(600, 900)  # 10 to 15 minutes

                print(f"[{time.strftime('%H:%M:%S')}] Backing off for {backoff_sec}s...")
                await asyncio.sleep(backoff_sec)

if __name__ == "__main__":
    asyncio.run(main())
