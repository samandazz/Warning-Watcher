import os
import time
import logging
from datetime import datetime
from typing import Optional, List, Tuple

import httpx
from bs4 import BeautifulSoup
import redis.asyncio as redis

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("warning-watcher")

# ---------------- ENV VARIABLES ----------------
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
ALLOWED_DOMAIN = [d.strip().lower() for d in os.getenv("ALLOWED_DOMAIN", "").split(",") if d.strip()]
REDIS_URL = os.getenv("REDIS_URL", "").strip()

DEFAULT_INTERVAL_MIN = int(os.getenv("DEFAULT_INTERVAL_MIN", "30"))
RETRY_BUDGET_SECONDS = int(os.getenv("RETRY_BUDGET_SECONDS", "180"))
RETRY_SLEEP_SECONDS = int(os.getenv("RETRY_SLEEP_SECONDS", "5"))
SCAN_LAST_N = int(os.getenv("SCAN_LAST_N", "5"))

# Load and Filter Empty Variables
RAW_IGNORE_LINES = [
    os.getenv("IGNORE_LINE_1"), os.getenv("IGNORE_LINE_2"), os.getenv("IGNORE_LINE_3"),
    os.getenv("IGNORE_LINE_OTP_1"), os.getenv("IGNORE_LINE_OTP_2"),
    os.getenv("IGNORE_LINE_MFA_1"), os.getenv("IGNORE_LINE_BILLING_1"),
    os.getenv("IGNORE_LINE_WORKSPACE_2"), os.getenv("IGNORE_LINE_USAGE_1")
]
# Only keep non-empty variables
IGNORE_LINES = [line for line in RAW_IGNORE_LINES if line and line.strip()]

# ---------------- REDIS SETUP ----------------
if not all([TG_TOKEN, ADMIN_IDS, ALLOWED_DOMAIN, REDIS_URL]):
    raise SystemExit("ERROR: Missing required env variables.")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://generator.email/",
}

# ---------------- KEY HELPER FUNCTIONS ----------------

def _normalize_text(text: str) -> str:
    """
    CRITICAL FIX: Removes all newlines, tabs, and extra spaces.
    Converts "Hello   \n World" -> "hello world"
    This ensures your long variables match even if the email has weird formatting.
    """
    if not text:
        return ""
    return " ".join(text.split()).lower()

def _is_allowed_domain(email: str) -> bool:
    email = (email or "").strip().lower()
    return any(email.endswith(f"@{d}") for d in ALLOWED_DOMAIN)

def _abs_url(href: str) -> str:
    href = (href or "").strip()
    if href.startswith("http"): return href
    return f"https://generator.email/{href.lstrip('/')}"

async def _sleep(seconds: int):
    import asyncio
    await asyncio.sleep(seconds)

# ---------------- TELEGRAM ----------------
async def tg_send_message(text: str, chat_id: int) -> bool:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False

async def alert_admins(text: str):
    for admin_id in ADMIN_IDS:
        await tg_send_message(text, admin_id)

# ---------------- SCRAPING ENGINE ----------------
def _extract_subject(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.title.get_text(" ", strip=True) if soup.title else ""

def _extract_email_body_text_from_page(soup: BeautifulSoup) -> str:
    """
    Tries specific IDs first. If failing, reads the whole body.
    Ensures we NEVER return empty string if there is content.
    """
    # 1. Try Specific ID/Class selectors (Best Match)
    selectors = ["email-body", "emailbody", "email_content", "message", "content", "mail"]
    
    for x in selectors:
        node = soup.find(id=x) or soup.find(class_=x)
        if node: return node.get_text(" ", strip=True)

    # 2. Try simple tags if structure is weird
    for tag in ['article', 'pre']:
        nodes = soup.find_all(tag)
        if nodes:
            biggest = max(nodes, key=lambda n: len(n.get_text()))
            return biggest.get_text(" ", strip=True)

    # 3. Fallback: Read EVERYTHING (Safety Net)
    if soup.body:
        return soup.body.get_text(" ", strip=True)
    
    return soup.get_text(" ", strip=True)

def _is_ignored(subject: str, body_text: str) -> bool:
    """
    Checks if any of your environment variables exist inside the email.
    Uses 'squash' normalization to ignore newlines and spacing issues.
    """
    # 1. Squash the email content into one clean line
    clean_email_content = _normalize_text(subject + " " + body_text)
    
    # 2. Check against every variable you set
    for raw_variable in IGNORE_LINES:
        # Squash the variable too so formats match
        clean_variable = _normalize_text(raw_variable)
        
        if clean_variable in clean_email_content:
            return True # ✅ MATCH! It is a known safe email.
            
    return False # ❌ NO MATCH! It is suspicious.

# ---------------- CHECK LOGIC ----------------
async def fetch_inbox_message_links(client: httpx.AsyncClient, email: str) -> List[str]:
    inbox_url = f"https://generator.email/{email}"
    r = await client.get(inbox_url, headers={**HEADERS, "Referer": "https://generator.email/"})
    r.raise_for_status()
    
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    table = soup.find(id="email-table")
    if table:
        for a in table.find_all("a", href=True):
            links.append(_abs_url(a["href"]))
            
    # Remove duplicates, keep order
    return list(dict.fromkeys(links))[:SCAN_LAST_N]

async def fetch_message_content(client: httpx.AsyncClient, msg_url: str) -> Tuple[str, str, str]:
    r = await client.get(msg_url, headers=HEADERS)
    r.raise_for_status()
    final_url = str(r.url)
    subject = _extract_subject(r.text)
    soup = BeautifulSoup(r.text, "html.parser")
    
    # Handle Iframes
    iframe = soup.find("iframe", src=True)
    if iframe:
        iframe_url = _abs_url(iframe["src"])
        ir = await client.get(iframe_url, headers=HEADERS)
        body_text = _extract_email_body_text_from_page(BeautifulSoup(ir.text, "html.parser"))
    else:
        body_text = _extract_email_body_text_from_page(soup)
        
    return subject, body_text, final_url

async def check_email_once(email: str) -> Tuple[str, Optional[str], Optional[str]]:
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        links = await fetch_inbox_message_links(client, email)
        if not links:
            return "NO_WARNING", None, None

        for msg_url in links:
            subject, body, final_url = await fetch_message_content(client, msg_url)

            if _is_ignored(subject, body):
                continue # This email is safe
            
            # If we get here, it's NOT safe -> WARNING
            return "WARNING", final_url, subject

        return "NO_WARNING", None, None

async def check_email_with_retry(email: str) -> Tuple[bool, str, Optional[str], Optional[str]]:
    start = time.time()
    while True:
        try:
            status, marker, subject = await check_email_once(email)
            return True, status, marker, subject
        except Exception as e:
            if time.time() - start >= RETRY_BUDGET_SECONDS:
                logger.error(f"[{email}] Retry budget exceeded: {e}")
                return False, "UNCONFIRMED", None, None
            await _sleep(RETRY_SLEEP_SECONDS)

# ---------------- MAIN LOOP ----------------
async def run_cycle():
    emails = await redis_client.smembers("warn:watchlist")
    valid_emails = [e for e in emails if _is_allowed_domain(e)]
    
    if not valid_emails:
        logger.info("Watchlist empty.")
        return

    logger.info(f"Checking {len(valid_emails)} inboxes...")
    
    for email in valid_emails:
        confirmed, status, url, subject = await check_email_with_retry(email)
        
        if not confirmed or status == "NO_WARNING":
            continue
            
        # HANDLE WARNING
        today = datetime.now().date().isoformat()
        marker = f"warning:{email}:{today}" 
        
        if await redis_client.get(f"warn:alerted:{email}") == marker:
            continue 

        msg = (
            f"🚨 ACCOUNT WARNING 🚨\n"
            f"📧 {email}\n"
            f"📝 Subject: {subject}\n"
            f"🔗 https://generator.email/{email}
        )
        
        await alert_admins(msg)
        await redis_client.set(f"warn:alerted:{email}", marker)
        logger.warning(f"Sent alert for {email}")

async def main():
    while True:
        try:
            await run_cycle()
        except Exception as e:
            logger.error(f"Cycle crash: {e}")
        
        interval = int(await redis_client.get("warn:interval_min") or DEFAULT_INTERVAL_MIN)
        logger.info(f"Sleeping {interval} min...")
        await _sleep(interval * 60)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
