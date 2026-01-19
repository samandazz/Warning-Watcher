import os
import time
import logging
import hashlib
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

# Interval (minutes): watcher reads from Redis key first, otherwise uses DEFAULT_INTERVAL_MIN
DEFAULT_INTERVAL_MIN = int(os.getenv("DEFAULT_INTERVAL_MIN", "30"))

# Network retry behavior (per email)
RETRY_BUDGET_SECONDS = int(os.getenv("RETRY_BUDGET_SECONDS", "180"))  # total retry time per email
RETRY_SLEEP_SECONDS = int(os.getenv("RETRY_SLEEP_SECONDS", "5"))      # sleep between retries
SCAN_LAST_N = int(os.getenv("SCAN_LAST_N", "5"))                      # scan newest N message links

# ✅ Ignore lines (old 3 + new ones)
IGNORE_LINE_1 = os.getenv("IGNORE_LINE_1", "").strip().lower()
IGNORE_LINE_2 = os.getenv("IGNORE_LINE_2", "").strip().lower()
IGNORE_LINE_3 = os.getenv("IGNORE_LINE_3", "").strip().lower()

IGNORE_LINE_OTP_1 = os.getenv("IGNORE_LINE_OTP_1", "").strip().lower()
IGNORE_LINE_OTP_2 = os.getenv("IGNORE_LINE_OTP_2", "").strip().lower()
IGNORE_LINE_MFA_1 = os.getenv("IGNORE_LINE_MFA_1", "").strip().lower()
IGNORE_LINE_BILLING_1 = os.getenv("IGNORE_LINE_BILLING_1", "").strip().lower()
IGNORE_LINE_WORKSPACE_2 = os.getenv("IGNORE_LINE_WORKSPACE_2", "").strip().lower()
IGNORE_LINE_USAGE_1 = os.getenv("IGNORE_LINE_USAGE_1", "").strip().lower()

# Redis keys (shared between OTP bot and watcher)
WATCHLIST_KEY = "warn:watchlist"          # Redis SET of emails
INTERVAL_KEY = "warn:interval_min"        # Redis STRING interval minutes
ALERTED_PREFIX = "warn:alerted:"          # Redis SET per email storing fingerprints

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

def _abs_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return "https://generator.email" + href
    return "https://generator.email/" + href

def _ignore_lines() -> List[str]:
    out = []
    for x in [
        IGNORE_LINE_1, IGNORE_LINE_2, IGNORE_LINE_3,
        IGNORE_LINE_OTP_1, IGNORE_LINE_OTP_2,
        IGNORE_LINE_MFA_1,
        IGNORE_LINE_BILLING_1,
        IGNORE_LINE_WORKSPACE_2,
        IGNORE_LINE_USAGE_1,
    ]:
        x = (x or "").strip().lower()
        if x:
            out.append(x)
    return out

def _fingerprint(subject: str, body_text: str) -> str:
    # stable per email content
    s = (subject or "").strip().lower()
    b = (body_text or "").strip().lower()
    raw = (s + "\n" + b).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:24]

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

async def fingerprint_already_alerted(email: str, fp: str) -> bool:
    if not fp:
        return False
    key = ALERTED_PREFIX + email
    try:
        return bool(await redis_client.sismember(key, fp))
    except Exception:
        return False

async def store_alerted_fingerprint(email: str, fp: str):
    if not fp:
        return
    key = ALERTED_PREFIX + email
    try:
        await redis_client.sadd(key, fp)
    except Exception:
        pass


# ---------------- generator.email scraping ----------------
def _extract_subject(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(" ", strip=True) if soup.title else ""

def _extract_email_body_text_from_page(soup: BeautifulSoup) -> str:
    """
    ✅ CRITICAL FIX:
    generator.email inbox pages contain sidebar/table with OTHER subjects.
    We must extract ONLY the email body container, not whole page text.
    """
    selectors = [
        {"id": "email-body"},
        {"id": "emailbody"},
        {"id": "email_content"},
        {"id": "email-content"},
        {"id": "messagebody"},
        {"id": "emailMessage"},
        {"id": "mail"},
        {"id": "content"},
    ]

    for sel in selectors:
        node = soup.find(id=sel["id"])
        if node:
            return node.get_text(" ", strip=True)

    class_candidates = [
        "email-body",
        "emailbody",
        "email-content",
        "message-body",
        "mailview",
        "content",
    ]
    for cls in class_candidates:
        node = soup.find(class_=cls)
        if node:
            return node.get_text(" ", strip=True)

    pres = soup.find_all("pre")
    if pres:
        biggest = max(pres, key=lambda x: len(x.get_text(" ", strip=True)))
        txt = biggest.get_text(" ", strip=True)
        if txt:
            return txt

    articles = soup.find_all("article")
    if articles:
        biggest = max(articles, key=lambda x: len(x.get_text(" ", strip=True)))
        txt = biggest.get_text(" ", strip=True)
        if txt:
            return txt

    return soup.get_text(" ", strip=True)

def _is_ignored(body_text: str) -> bool:
    t = (body_text or "").lower()
    for line in _ignore_lines():
        if line and line in t:
            return True
    return False

async def fetch_inbox_message_links(client: httpx.AsyncClient, email: str) -> List[str]:
    inbox_url = f"https://generator.email/{email}"
    r = await client.get(inbox_url, headers={**HEADERS, "Referer": "https://generator.email/"})
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    links: List[str] = []
    table = soup.find(id="email-table")
    if table:
        for a in table.find_all("a", href=True):
            links.append(_abs_url(a["href"]))

    out, seen = [], set()
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)

    return out[:SCAN_LAST_N]

async def fetch_message_subject_and_body(client: httpx.AsyncClient, inbox_url: str, msg_url: str) -> Tuple[str, str, str]:
    """
    Returns: subject, body_text, final_url
    final_url is important because msg_url redirects to /inboxX/
    """
    r = await client.get(msg_url, headers={**HEADERS, "Referer": inbox_url})
    r.raise_for_status()

    final_url = str(r.url)
    subject = _extract_subject(r.text)
    soup = BeautifulSoup(r.text, "html.parser")

    iframe = soup.find("iframe", src=True)
    if iframe and iframe.get("src"):
        iframe_url = _abs_url(iframe["src"].strip())
        ir = await client.get(iframe_url, headers={**HEADERS, "Referer": final_url})
        ir.raise_for_status()

        iframe_soup = BeautifulSoup(ir.text, "html.parser")
        body_text = _extract_email_body_text_from_page(iframe_soup)
        return subject, body_text, final_url

    body_text = _extract_email_body_text_from_page(soup)
    return subject, body_text, final_url


async def check_email_once(email: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Returns:
      ("WARNING", inbox_url, subject, fingerprint)
      ("NO_WARNING", None, None, None)
    """
    inbox_url = f"https://generator.email/{email}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0), follow_redirects=True) as client:
        links = await fetch_inbox_message_links(client, email)

        if not links:
            return "NO_WARNING", None, None, None

        for msg_url in links:
            subject, body_text, final_url = await fetch_message_subject_and_body(client, inbox_url, msg_url)

            if _is_ignored(body_text):
                logger.info(f"[{email}] ignored email matched ignore lines (subject='{subject}').")
                continue

            fp = _fingerprint(subject, body_text)

            # ✅ If this exact issue email already alerted, skip it
            if await fingerprint_already_alerted(email, fp):
                logger.info(f"[{email}] already alerted same issue email (subject='{subject}').")
                continue

            # ✅ First non-ignored + not-seen email among top N => WARNING
            return "WARNING", inbox_url, subject, fp

        return "NO_WARNING", None, None, None

async def check_email_with_retry(email: str) -> Tuple[bool, str, Optional[str], Optional[str], Optional[str]]:
    """
    Returns:
      confirmed, status, inbox_url, subject, fingerprint
    If confirmed=False => UNCONFIRMED (network issues exceeded budget)
    """
    start = time.time()
    attempt = 0

    while True:
        attempt += 1
        try:
            status, inbox_url, subject, fp = await check_email_once(email)
            return True, status, inbox_url, subject, fp
        except httpx.HTTPError as e:
            elapsed = time.time() - start
            logger.warning(f"[{email}] network error attempt={attempt}: {e}")

            if elapsed >= RETRY_BUDGET_SECONDS:
                return False, "UNCONFIRMED", None, None, None

            await _sleep(RETRY_SLEEP_SECONDS)


# ---------------- Main cycle ----------------
async def run_cycle():
    emails = await get_watchlist()
    if not emails:
        logger.info("Watchlist empty. Nothing to check.")
        return

    logger.info(f"Cycle start: checking {len(emails)} inbox(es).")

    for email in emails:
        confirmed, status, inbox_url, subject, fp = await check_email_with_retry(email)

        # ✅ Do NOT notify admins on network issues; just log and retry next cycle.
        if not confirmed:
            logger.warning(f"[{email}] could not confirm inbox due to network issues; will retry next cycle.")
            continue

        if status == "NO_WARNING":
            logger.info(f"[{email}] confirmed: no warning.")
            continue

        # WARNING found (new issue email)
        subj_line = f"🧾 Subject: {subject}\n" if subject else ""
        msg = (
            "🚨 ACCOUNT WARNING DETECTED 🚨\n"
            f"📧 {email}\n"
            f"{subj_line}"
            f"🔗 Inbox: {inbox_url}\n"
            f"🕒 {datetime.now().isoformat()}"
        ).strip()

        await alert_admins(msg)

        # ✅ store fingerprint so same issue email will NOT alert again
        await store_alerted_fingerprint(email, fp)

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
