import os
import time
import logging
from datetime import datetime
from typing import Optional, List, Tuple

import httpx
from bs4 import BeautifulSoup
import redis.asyncio as redis

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("warning-watcher")

# ---------------- ENV (Railway Variables) ----------------
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

ALLOWED_DOMAIN = [d.strip().lower() for d in os.getenv("ALLOWED_DOMAIN", "").split(",") if d.strip()]
REDIS_URL = os.getenv("REDIS_URL", "").strip()

DEFAULT_INTERVAL_MIN = int(os.getenv("DEFAULT_INTERVAL_MIN", "30"))

RETRY_BUDGET_SECONDS = int(os.getenv("RETRY_BUDGET_SECONDS", "180"))
RETRY_SLEEP_SECONDS = int(os.getenv("RETRY_SLEEP_SECONDS", "5"))
SCAN_LAST_N = int(os.getenv("SCAN_LAST_N", "10"))

# ✅ Ignore lines (exact fragments)
IGNORE_LINE_1 = os.getenv("IGNORE_LINE_1", "").strip().lower()
IGNORE_LINE_2 = os.getenv("IGNORE_LINE_2", "").strip().lower()
IGNORE_LINE_3 = os.getenv("IGNORE_LINE_3", "").strip().lower()

WATCHLIST_KEY = "warn:watchlist"
INTERVAL_KEY = "warn:interval_min"
ALERTED_PREFIX = "warn:alerted:"  # per email last alerted message url

# ---------------- Validation ----------------
if not TG_TOKEN:
    raise SystemExit("ERROR: TG_TOKEN is required (Railway variable).")
if not ADMIN_IDS:
    raise SystemExit("ERROR: ADMIN_IDS is required (comma-separated Telegram IDs).")
if not ALLOWED_DOMAIN:
    raise SystemExit("ERROR: ALLOWED_DOMAIN is required (comma-separated domains).")
if not REDIS_URL:
    raise SystemExit("ERROR: REDIS_URL is required (from Railway Redis service).")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Referer": "https://generator.email/",
}


# ---------------- Utils ----------------
def _is_allowed_domain(email: str) -> bool:
    email = (email or "").strip().lower()
    return any(email.endswith(f"@{d}") for d in ALLOWED_DOMAIN)

def _allowed_ignore_lines() -> List[str]:
    out = []
    for x in [IGNORE_LINE_1, IGNORE_LINE_2, IGNORE_LINE_3]:
        x = (x or "").strip().lower()
        if x:
            out.append(x)
    return out

def _abs_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://generator.email" + href
    return "https://generator.email/" + href

async def _sleep(seconds: int):
    import asyncio
    await asyncio.sleep(seconds)


# ---------------- Telegram send (NO polling) ----------------
async def tg_send_message(text: str, chat_id: int) -> bool:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            return bool(data.get("ok"))
    except Exception as e:
        logger.error(f"Telegram send failed to {chat_id}: {e}")
        return False

async def alert_admins(text: str):
    for admin_id in ADMIN_IDS:
        await tg_send_message(text, admin_id)


# ---------------- Redis read/write ----------------
async def get_interval_min() -> int:
    v = await redis_client.get(INTERVAL_KEY)
    if not v:
        return DEFAULT_INTERVAL_MIN
    try:
        n = int(v)
        return n if n > 0 else DEFAULT_INTERVAL_MIN
    except ValueError:
        return DEFAULT_INTERVAL_MIN

async def get_watchlist() -> List[str]:
    emails = await redis_client.smembers(WATCHLIST_KEY)
    out = []
    for e in sorted(list(emails)):
        e = (e or "").strip().lower()
        if e and _is_allowed_domain(e):
            out.append(e)
    return out

async def get_alerted_marker(email: str) -> Optional[str]:
    return await redis_client.get(ALERTED_PREFIX + email)

async def set_alerted_marker(email: str, marker: str):
    await redis_client.set(ALERTED_PREFIX + email, marker)


# ---------------- generator.email reading ----------------
def _extract_subject(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(" ", strip=True) if soup.title else ""

async def fetch_message_links(client: httpx.AsyncClient, email: str) -> List[str]:
    inbox_url = f"https://generator.email/{email}"
    r = await client.get(inbox_url, headers={**HEADERS, "Referer": "https://generator.email/"})
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    links = []
    email_table = soup.find(id="email-table")
    if email_table:
        for a in email_table.find_all("a", href=True):
            links.append(_abs_url(a["href"]))

    out, seen = [], set()
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)

    return out[:max(1, SCAN_LAST_N)]


async def fetch_full_message_text(client: httpx.AsyncClient, msg_url: str, inbox_url: str) -> Tuple[str, str]:
    """
    ✅ FIX: Ignore-check must only use REAL email body.
    generator.email page includes inbox list + other subjects -> false ignore.
    So:
      - subject from <title>
      - body from iframe ONLY if iframe exists
      - fallback body from page text if no iframe
    """
    msg_resp = await client.get(msg_url, headers={**HEADERS, "Referer": inbox_url})
    msg_resp.raise_for_status()

    subject = _extract_subject(msg_resp.text)
    msg_soup = BeautifulSoup(msg_resp.text, "html.parser")

    iframe = msg_soup.find("iframe", src=True)
    if iframe and iframe.get("src"):
        iframe_url = _abs_url(iframe["src"].strip())
        iframe_resp = await client.get(iframe_url, headers={**HEADERS, "Referer": msg_url})
        iframe_resp.raise_for_status()
        body_text = BeautifulSoup(iframe_resp.text, "html.parser").get_text(" ", strip=True)
        return subject, body_text

    # fallback (rare): no iframe
    body_text = msg_soup.get_text(" ", strip=True)
    return subject, body_text


def is_ignored_email(body_text: str) -> bool:
    t = (body_text or "").lower()
    for line in _allowed_ignore_lines():
        if line and line in t:
            return True
    return False


async def check_email_once(email: str) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Returns:
      ("WARNING", marker_url, subject)
      ("NO_WARNING", None, None)
    """
    inbox_url = f"https://generator.email/{email}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        links = await fetch_message_links(client, email)

        if not links:
            return "NO_WARNING", None, None

        for msg_url in links:
            subject, body_text = await fetch_full_message_text(client, msg_url, inbox_url)

            if is_ignored_email(body_text):
                logger.info(f"[{email}] ignored email matched ignore lines (subject='{subject}').")
                continue

            return "WARNING", msg_url, subject

        return "NO_WARNING", None, None


async def check_email_with_retry(email: str) -> Tuple[bool, str, Optional[str], Optional[str]]:
    start = time.time()
    attempt = 0

    while True:
        attempt += 1
        try:
            status, marker, subject = await check_email_once(email)
            return True, status, marker, subject
        except httpx.HTTPError as e:
            elapsed = time.time() - start
            logger.warning(f"[{email}] network error attempt={attempt}: {e}")

            if elapsed >= RETRY_BUDGET_SECONDS:
                return False, "UNCONFIRMED", None, None

            await _sleep(RETRY_SLEEP_SECONDS)


# ---------------- Main cycle ----------------
async def run_cycle():
    emails = await get_watchlist()
    if not emails:
        logger.info("Watchlist empty. Nothing to check.")
        return

    logger.info(f"Cycle start: checking {len(emails)} inbox(es).")

    for email in emails:
        confirmed, status, marker, subject = await check_email_with_retry(email)

        # ✅ do not message admins for network errors
        if not confirmed:
            logger.warning(f"[{email}] could not confirm inbox due to network issues; will retry next cycle.")
            continue

        if status == "NO_WARNING":
            logger.info(f"[{email}] confirmed: no warning.")
            continue

        marker = marker or f"warning:{email}:{datetime.now().date().isoformat()}"
        prev = await get_alerted_marker(email)
        if prev == marker:
            logger.info(f"[{email}] warning already alerted (marker unchanged).")
            continue

        subj_line = f"🧾 Subject: {subject}\n" if subject else ""
        msg = (
            "🚨 ACCOUNT WARNING DETECTED 🚨\n"
            f"📧 {email}\n"
            f"{subj_line}"
            f"🔗 Message: {marker}\n"
            f"🕒 {datetime.now().isoformat()}"
        ).strip()

        await alert_admins(msg)
        await set_alerted_marker(email, marker)
        logger.info(f"[{email}] alerted admins.")


async def main_loop():
    while True:
        try:
            await run_cycle()
        except Exception as e:
            logger.error(f"Cycle crash: {e}")

        interval = await get_interval_min()
        sleep_s = max(1, interval) * 60
        logger.info(f"Cycle done. Sleeping {sleep_s}s (interval={interval}m).")
        await _sleep(sleep_s)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main_loop())
