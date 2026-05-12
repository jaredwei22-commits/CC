import feedparser
import smtplib
import os
import sys
import socket
import re
import textwrap
from email.mime.text import MIMEText
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ──────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE   = SCRIPT_DIR / ".env"

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT   = 587
RECIPIENT   = "jaredwei22@gmail.com"
MAX_PER_FEED = 3

# RSS feeds — picked for reliable, paywall-free summary fields
FEEDS: list[tuple[str, str]] = [
    ("BBC World",     "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("The Guardian",  "https://www.theguardian.com/world/rss"),
    ("NPR World",     "https://feeds.npr.org/1004/rss.xml"),
    ("Al Jazeera",    "https://www.aljazeera.com/xml/rss/all.xml"),
    ("ABC News",      "https://abcnews.go.com/abcnews/internationalheadlines"),
    ("CBS World",     "https://www.cbsnews.com/latest/rss/world"),
]


# ── Helpers ──────────────────────────────────────────────────────

def load_env() -> dict[str, str]:
    config: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                config[key.strip()] = val.strip()
    if not config.get("GMAIL_USER") or not config.get("GMAIL_APP_PASSWORD"):
        print("[ERROR] Missing GMAIL_USER or GMAIL_APP_PASSWORD in .env")
        sys.exit(1)
    return config


def setup_proxy(config: dict[str, str]):
    socks5 = config.get("SOCKS5_PROXY", "")
    if socks5:
        host, _, port_str = socks5.partition(":")
        port = int(port_str) if port_str else 1080
        try:
            import socks
            socks.set_default_proxy(socks.SOCKS5, host, port)
            socket.socket = socks.socksocket
            print(f"[INFO] Using SOCKS5 proxy {host}:{port}")
        except ImportError:
            print("[ERROR] SOCKS5 proxy configured but 'pysocks' is not installed.")
            print("        Run: pip install pysocks")
            sys.exit(1)


# ── Text helpers ─────────────────────────────────────────────────

_HTML_RE = re.compile(r"<[^>]*>")
_ENTITY_RE = re.compile(r"&[a-zA-Z]+;|&#\d+;")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = _HTML_RE.sub(" ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&apos;", "'")
    text = text.replace("&nbsp;", " ").replace("&rsquo;", "'").replace("&lsquo;", "'")
    text = text.replace("&rdquo;", '"').replace("&ldquo;", '"')
    text = _ENTITY_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def wrap_text(text: str, width: int = 70) -> str:
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False))


# ── News fetching ────────────────────────────────────────────────

def get_rss_summary(entry) -> str:
    """Extract the best available summary from an RSS entry."""
    # summary is the most common field; description is an alias in some feeds
    raw = entry.get("summary", entry.get("description", ""))
    if not raw:
        return ""

    text = strip_html(raw)

    # The Guardian tends to duplicate title in summary — strip it
    title = (entry.get("title") or "").strip()
    if title and text.startswith(title):
        text = text[len(title):].strip()

    return text


def fetch_articles() -> list[tuple[str, str, str]]:
    """Fetch headlines + summaries from RSS feeds (no external fetch needed)."""
    results: list[tuple[str, str, str]] = []
    seen_titles: set[str] = set()

    for source, url in FEEDS:
        count = 0
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[WARN] {source}: parse error — {e}")
            continue

        for entry in feed.entries:
            if count >= MAX_PER_FEED:
                break
            title = (entry.get("title") or "").strip()
            if not title or title in seen_titles:
                continue

            summary = get_rss_summary(entry)

            # If RSS has no summary, try trafilatura as fallback
            if not summary:
                link = (entry.get("link") or "").strip()
                summary = fallback_extract(link)

            if not summary:
                summary = "(No description available)"

            seen_titles.add(title)
            results.append((source, title, wrap_text(summary)))
            count += 1

    print(f"  -> {len(results)} articles from {len(FEEDS)} sources")
    return results


def fallback_extract(url: str) -> str:
    """Last-resort: try to download article text via trafilatura."""
    if not url:
        return ""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url, timeout=10)
        if downloaded:
            text = trafilatura.extract(downloaded, include_links=False,
                                       include_images=False, include_tables=False,
                                       output_format="txt")
            return (text or "").strip()
    except Exception:
        pass
    return ""


# ── Email builders ───────────────────────────────────────────────

def build_email(articles: list[tuple[str, str, str]]) -> str:
    date_str = datetime.now().strftime("%A, %B %d, %Y")
    sep = "─" * 60

    lines = [
        f"DAILY BRIEFING  —  {date_str}",
        "=" * 60,
        "",
    ]

    for i, (source, title, body) in enumerate(articles):
        lines.append(f"[{source}] {title}")
        lines.append("")
        for line in body.split("\n"):
            lines.append(f"  {line}")
        lines.append("")
        if i < len(articles) - 1:
            lines.append(sep)
            lines.append("")

    return "\n".join(lines)


# ── Email sending ────────────────────────────────────────────────

def send_email(user: str, password: str, body: str):
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]    = user
    msg["To"]      = RECIPIENT
    msg["Subject"] = f"Daily Briefing — {datetime.now().strftime('%B %d, %Y')}"

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(user, password)
            server.sendmail(user, RECIPIENT, msg.as_string())
        print(f"[OK] Email sent to {RECIPIENT}")
    except smtplib.SMTPAuthenticationError:
        print("[ERROR] Gmail authentication failed. Check app password in .env")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Failed to send email: {e}")
        sys.exit(1)


# ── Main ─────────────────────────────────────────────────────────

def main():
    config = load_env()
    setup_proxy(config)

    print("Fetching news...")
    articles = fetch_articles()

    if not articles:
        print("[WARN] No articles fetched. Aborting.")
        sys.exit(1)

    body = build_email(articles)
    send_email(config["GMAIL_USER"], config["GMAIL_APP_PASSWORD"], body)


if __name__ == "__main__":
    main()
