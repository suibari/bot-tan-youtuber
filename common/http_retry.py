"""外部データ取得用の有限再試行。投稿・アップロードには使用しない。"""

import math
import time
from email.utils import parsedate_to_datetime

import requests


def retry_delay(headers, default=61.0) -> float:
    """指定された解除時刻を尊重し、不明なら1分待つ。"""
    delays = []
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = float("nan")
        if math.isfinite(delay):
            delays.append(max(0, delay))
    try:
        reset = float(headers.get("X-RateLimit-Reset", "nan")) - time.time()
        if math.isfinite(reset):
            delays.append(max(0, reset))
    except (ValueError, TypeError):
        pass
    return max(1, max(delays) + 1) if delays else default



def request(call, url, *, label, **kwargs):
    """読み取りAPI（検索POSTを含む）を最大3回。秘密情報や本文はログに出さない。"""
    for attempt in range(1, 4):
        response = None
        try:
            response = call(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.SSLError:
            # 証明書不良は待っても解消しない。
            raise
        except (requests.Timeout, requests.ConnectionError,
                requests.exceptions.ChunkedEncodingError, requests.HTTPError) as error:
            if response is not None:
                status = response.status_code
                if status not in {408, 429, 500, 502, 503, 504}:
                    raise
                reason = f"HTTP {status}"
                delay = retry_delay(response.headers, default=61.0 if status == 429 else 2 ** attempt)
            else:
                reason = type(error).__name__
                delay = 2 ** attempt
            if attempt == 3 or delay > 300:
                print(f"[{label}] {reason} 試行{attempt}/3: 再試行を終了します")
                raise
            print(f"[{label}] {reason} 試行{attempt}/3: {delay:.1f}秒待って再試行します")
            time.sleep(delay)
