#!/usr/bin/env python3
"""
Delete OpenAI response logs (responses) by API key.

This script:
1. Loads response IDs from logs.csv in the same folder
2. Deletes them with adaptive concurrency and 429 backoff
3. Handles errors and reports progress

Usage:
    python3 docker/delete_openai_logs.py --api-key sk_... [--dry-run]

Options:
    --api-key KEY     OpenAI API key (required)
    --concurrency N   Max delete requests to run at once (default: 8)
    --max-retries N   Retry attempts per response after a 429 (default: 5)
    --dry-run         List responses but don't delete them
    --limit N         Only fetch first N responses (useful for testing)
"""

import json
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import urllib.error
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_CSV_PATH = SCRIPT_DIR / "logs.csv"


class AdaptiveRateLimiter:
    """Shared pacing state that slows down on 429s and recovers on success."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0
        self._current_delay = 0.0
        self._last_backoff_at = 0.0
        self._last_decay_at = time.monotonic()
        self._min_delay = 0.0
        self._max_delay = 6.0
        self._backoff_step = 0.5
        self._backoff_cooldown = 1.5
        self._decay_step = 0.2
        self._decay_interval = 2.0

    def _apply_time_decay_locked(self, now: float) -> None:
        """Recover speed gradually over time while work is ongoing."""
        if self._current_delay <= self._min_delay:
            self._last_decay_at = now
            return

        elapsed = now - self._last_decay_at
        if elapsed < self._decay_interval:
            return

        steps = int(elapsed // self._decay_interval)
        self._current_delay = max(self._min_delay, self._current_delay - (steps * self._decay_step))
        self._last_decay_at += steps * self._decay_interval

    def reserve_slot(self) -> None:
        """Wait until this worker may start the next request."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._apply_time_decay_locked(now)
                wait_for = self._next_allowed_at - now
                if wait_for <= 0:
                    self._next_allowed_at = now + self._current_delay
                    return

            time.sleep(min(wait_for, 0.5))

    def record_success(self) -> None:
        """Gradually reduce the pacing delay after clean responses."""
        with self._lock:
            now = time.monotonic()
            self._apply_time_decay_locked(now)
            if self._current_delay > self._min_delay:
                self._current_delay = max(self._min_delay, self._current_delay - 0.05)

    def record_rate_limit(self, retry_after: float | None = None) -> tuple[float, bool]:
        """Slow down on 429 and return (new_delay, delay_increased)."""
        with self._lock:
            now = time.monotonic()
            self._apply_time_decay_locked(now)

            base_delay = retry_after if retry_after is not None else 0.8
            previous_delay = self._current_delay

            if now - self._last_backoff_at >= self._backoff_cooldown:
                if self._current_delay < base_delay:
                    self._current_delay = min(self._max_delay, base_delay)
                else:
                    self._current_delay = min(self._max_delay, self._current_delay + self._backoff_step)
                self._last_backoff_at = now
            else:
                # During cooldown, avoid compounding backoff from concurrent 429 responses.
                self._current_delay = min(self._max_delay, max(self._current_delay, base_delay))

            if self._current_delay > previous_delay:
                self._next_allowed_at = max(self._next_allowed_at, now + self._current_delay)
                return self._current_delay, True

            return self._current_delay, False


def parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None

    try:
        return max(0.0, float(header_value.strip()))
    except ValueError:
        return None


def load_request_ids_from_csv(csv_path: Path) -> list[str]:
    """Load only the first column (Request ID) from logs.csv."""
    if not csv_path.is_file():
        raise FileNotFoundError(f"Missing CSV file: {csv_path}")

    request_ids: list[str] = []
    with csv_path.open(encoding="utf-8-sig") as csv_file:
        header = csv_file.readline().strip()
        if not header.startswith("Request ID,"):
            raise ValueError('CSV file must start with a "Request ID" column')

        for line in csv_file:
            first_cell = line.partition(",")[0].strip()
            if first_cell.startswith('"') and first_cell.endswith('"') and len(first_cell) >= 2:
                first_cell = first_cell[1:-1]
            if first_cell:
                request_ids.append(first_cell)

    return request_ids


def delete_openai_response(api_key: str, response_id: str) -> bool:
    """
    Delete a single response by ID.
    
    Returns True if successful, False if already deleted or not found.
    Raises exception on other errors.
    """
    url = f"https://api.openai.com/v1/responses/{response_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    try:
        req = urllib.request.Request(url, headers=headers, method="DELETE")
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data.get("deleted", False)
    
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Already deleted or doesn't exist
            return False
        raise


def delete_openai_response_with_retry(
    api_key: str,
    response_id: str,
    rate_limiter: AdaptiveRateLimiter,
    max_retries: int,
) -> bool:
    """Delete a response, retrying 429s with shared adaptive backoff."""
    for attempt in range(1, max_retries + 1):
        rate_limiter.reserve_slot()

        try:
            success = delete_openai_response(api_key, response_id)
            rate_limiter.record_success()
            return success

        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = parse_retry_after(e.headers.get("Retry-After"))
                delay, increased = rate_limiter.record_rate_limit(retry_after)
                if attempt < max_retries:
                    if increased:
                        print(
                            f"    ↺ 429 for {response_id}; backoff now {delay:.2f}s "
                            f"before retry {attempt + 1}/{max_retries}"
                        )
                    else:
                        print(
                            f"    ↺ 429 for {response_id}; retrying under current backoff {delay:.2f}s "
                            f"({attempt + 1}/{max_retries})"
                        )
                    continue
            raise


def delete_responses_batch(
    api_key: str,
    response_ids: list[str],
    dry_run: bool = False,
    concurrency: int = 8,
    max_retries: int = 5,
) -> dict:
    """
    Delete responses in batches.
    
    Args:
        api_key: OpenAI API key
        response_ids: List of response IDs to delete
        dry_run: If True, don't actually delete, just report what would be deleted
        concurrency: Number of delete requests to run at once
        max_retries: Retry attempts per response after a 429
    
    Returns:
        Dictionary with deletion stats
    """
    stats = {
        "total": len(response_ids),
        "deleted": 0,
        "failed": 0,
        "skipped": 0,  # Already deleted / not found
        "errors": []
    }
    
    if dry_run:
        print(f"\n[DRY RUN] Would delete {len(response_ids)} response(s)")
        if response_ids:
            print(f"  1. {response_ids[0]}")
            if len(response_ids) > 1:
                print(f"  ... and {len(response_ids) - 1} more")
        return stats
    
    print(f"\nDeleting {len(response_ids)} response(s) with max concurrency {concurrency}...")

    max_workers = max(1, min(concurrency, len(response_ids)))
    rate_limiter = AdaptiveRateLimiter()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(
                delete_openai_response_with_retry,
                api_key,
                response_id,
                rate_limiter,
                max_retries,
            ): (index, response_id)
            for index, response_id in enumerate(response_ids, 1)
        }

        for future in as_completed(future_to_index):
            i, response_id = future_to_index[future]
            try:
                success = future.result()

                if success:
                    stats["deleted"] += 1
                    print(f"  ✓ {i}/{len(response_ids)}: {response_id} deleted")
                else:
                    stats["skipped"] += 1
                    print(f"  - {i}/{len(response_ids)}: {response_id} (already deleted/not found)")

            except Exception as e:
                stats["failed"] += 1
                error_msg = f"{response_id}: {str(e)}"
                stats["errors"].append(error_msg)
                print(f"  ✗ {i}/{len(response_ids)}: {response_id} - ERROR: {e}")
    
    return stats


def print_stats(stats: dict) -> None:
    """Print deletion statistics."""
    print("\n" + "="*60)
    print("DELETION SUMMARY")
    print("="*60)
    print(f"Total responses:        {stats['total']}")
    print(f"Successfully deleted:   {stats['deleted']}")
    print(f"Skipped (not found):    {stats['skipped']}")
    print(f"Failed:                 {stats['failed']}")
    
    if stats["errors"]:
        print("\nErrors:")
        for error in stats["errors"]:
            print(f"  - {error}")
    
    print("="*60)


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Delete OpenAI response logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="OpenAI API key (required)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List responses but don't delete them"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Max delete requests to run at once (default: 8)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Retry attempts per response after a 429 (default: 5)"
    )
    args = parser.parse_args()
    
    try:
        api_key = args.api_key
        
        # Load response IDs from the local CSV file.
        response_ids = load_request_ids_from_csv(LOGS_CSV_PATH)
        
        if not response_ids:
            print(f"No request IDs found in {LOGS_CSV_PATH}.")
            return 0
        
        print(f"\nFound {len(response_ids)} response(s)")
        
        # Delete in batches
        stats = delete_responses_batch(
            api_key,
            response_ids,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
            max_retries=args.max_retries,
        )
        
        # Print summary
        print_stats(stats)
        
        # Exit with error code if there were failures
        if stats["failed"] > 0:
            return 1
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
