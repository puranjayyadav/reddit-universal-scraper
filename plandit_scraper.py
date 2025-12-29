"""
Reliable Reddit scraper with JSON persistence and logging (no API credentials required).

Uses Reddit's public JSON endpoints to fetch new posts, retries on failures without
crashing, deduplicates against prior runs, and stores results in a daily JSON file
named `plandit_data_YYYY-MM-DD.json`.
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import requests
from requests import Response
from requests.exceptions import RequestException

LOG_FILE = Path("scraper.log")
USER_AGENT = "plandit-scraper/1.0 (+https://github.com/ksanjeev284/reddit-universal-scraper)"
BASE_URL = "https://old.reddit.com"


def configure_logging() -> None:
    """Configure logging to file and console."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def output_path() -> Path:
    """Return the daily JSON output path."""
    date_str = datetime.now(timezone.utc).date().isoformat()
    return Path(f"plandit_data_{date_str}.json")


def load_existing_posts(path: Path) -> Tuple[List[dict], Set[str]]:
    """
    Load existing posts and their IDs from the JSON file if it exists.

    Returns a tuple of (records, post_ids).
    """
    if not path.exists():
        return [], set()

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            if not isinstance(data, list):
                logging.warning("Existing data file is not a list; starting fresh.")
                return [], set()
    except json.JSONDecodeError:
        logging.error("Existing JSON file is invalid. Starting with an empty dataset.")
        return [], set()

    post_ids = {
        record.get("post_id")
        for record in data
        if isinstance(record, dict) and record.get("post_id")
    }
    return data, post_ids


def persist_posts(path: Path, records: List[dict]) -> None:
    """Persist posts to disk with indentation for readability."""
    with path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=4)
    logging.info("Saved %s total posts to %s", len(records), path)


def fetch_posts_with_retry(
    subreddit: str,
    limit: int,
    wait_seconds: int,
    max_attempts: int | None,
    session: requests.Session,
) -> Iterable[dict]:
    """Fetch subreddit posts with retry and cooldown on errors."""
    attempts = 0
    url = f"{BASE_URL}/r/{subreddit}/new.json?limit={limit}&raw_json=1"

    while True:
        attempts += 1
        try:
            logging.info("Fetching up to %s posts from r/%s (attempt %s)", limit, subreddit, attempts)
            response: Response = session.get(url, timeout=15)
            if response.status_code == 200:
                data = response.json()
                children = data.get("data", {}).get("children", [])
                return [child.get("data", {}) for child in children if isinstance(child, dict)]

            logging.error("Request failed (status=%s). Body: %s", response.status_code, response.text[:200])
        except RequestException as exc:
            logging.error("Network error fetching posts: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logging.error("Unexpected error during fetch: %s", exc)

        if max_attempts is not None and attempts >= max_attempts:
            logging.error("Max attempts reached (%s). Exiting without crashing.", max_attempts)
            return []

        logging.info("Retrying in %s seconds...", wait_seconds)
        time.sleep(wait_seconds)


def extract_submission(raw: dict) -> dict:
    """Normalize a raw reddit post to a serializable dict."""
    created = raw.get("created_utc")
    created_iso = (
        datetime.fromtimestamp(created, tz=timezone.utc).isoformat() if created else None
    )
    return {
        "post_id": raw.get("id"),
        "title": raw.get("title"),
        "score": raw.get("score"),
        "created_utc": created_iso,
        "subreddit": raw.get("subreddit"),
        "author": raw.get("author"),
        "url": raw.get("url"),
        "permalink": f"https://reddit.com{raw.get('permalink')}" if raw.get("permalink") else None,
        "num_comments": raw.get("num_comments"),
        "over_18": raw.get("over_18"),
        "selftext": raw.get("selftext"),
    }


def should_keep_submission(
    raw: dict, min_score: int | None, keyword: str | None
) -> bool:
    """Determine if a submission passes optional filters."""
    post_id = raw.get("id")
    if min_score is not None and (raw.get("score") or 0) < min_score:
        logging.info("Skipped post %s below min_score=%s", post_id, min_score)
        return False

    if keyword:
        text = f"{raw.get('title', '')} {raw.get('selftext', '')}".lower()
        if keyword.lower() not in text:
            logging.info("Skipped post %s missing keyword '%s'", post_id, keyword)
            return False

    return True


def scrape(
    subreddit: str,
    limit: int,
    min_score: int | None,
    keyword: str | None,
    retry_wait: int,
    max_retries: int | None,
) -> None:
    """Scrape a subreddit with persistence, deduplication, and logging."""
    configure_logging()
    logging.info("Starting scrape for r/%s with limit=%s", subreddit, limit)

    destination = output_path()
    existing_records, existing_ids = load_existing_posts(destination)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    posts = fetch_posts_with_retry(
        subreddit=subreddit,
        limit=limit,
        wait_seconds=retry_wait,
        max_attempts=max_retries,
        session=session,
    )

    new_records: List[dict] = []
    for raw in posts:
        if not should_keep_submission(raw, min_score, keyword):
            continue

        post_id = raw.get("id")
        if post_id in existing_ids:
            logging.info("Skipped duplicate post %s", post_id)
            continue

        record = extract_submission(raw)
        new_records.append(record)
        existing_ids.add(post_id)
        logging.info("Scraped post %s", post_id)

    if new_records:
        persist_posts(destination, existing_records + new_records)
    else:
        logging.info("No new posts to save for %s.", subreddit)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Scrape subreddit posts with persistence and retries (no API keys)."
    )
    parser.add_argument("subreddit", help="Subreddit name (without r/ prefix).")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of posts to fetch (default: 50).",
    )
    parser.add_argument(
        "--min-score",
        type=int,
        default=None,
        help="Minimum score filter for posts.",
    )
    parser.add_argument(
        "--keyword",
        type=str,
        default=None,
        help="Keyword that must appear in the title or selftext.",
    )
    parser.add_argument(
        "--retry-wait",
        type=int,
        default=60,
        help="Seconds to wait before retrying on failure (default: 60).",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Maximum retry attempts before exiting. Omit for unlimited retries.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        scrape(
            subreddit=arguments.subreddit,
            limit=arguments.limit,
            min_score=arguments.min_score,
            keyword=arguments.keyword,
            retry_wait=arguments.retry_wait,
            max_retries=arguments.max_retries,
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("Scrape terminated due to unrecoverable error: %s", exc)
        sys.exit(1)
