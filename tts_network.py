"""Bounded retry policy shared by downloads and online synthesis."""
import email.utils
import http.client
import time
import urllib.error
import urllib.request
from tts_config import network_timeout


def transient(error):
    status = getattr(error, 'code', getattr(error, 'status', None))
    if status is not None:
        return status in (408, 429, 500, 502, 503, 504)
    return isinstance(error, (urllib.error.URLError, ConnectionError, TimeoutError, OSError, http.client.IncompleteRead))


def retry_after(error, attempt):
    headers = getattr(error, 'headers', None)
    value = headers.get('Retry-After') if headers else None
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = email.utils.parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        return max(0, min(seconds, 60))
    return min(0.5 * 2 ** attempt, 4)


def retry(operation, attempts=4):
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as error:
            if attempt + 1 == attempts or not transient(error):
                raise
            time.sleep(retry_after(error, attempt))


def read_url(request, timeout=None):
    def receive():
        with urllib.request.urlopen(request, timeout=timeout or network_timeout()) as response:
            return response.read()
    return retry(receive)
