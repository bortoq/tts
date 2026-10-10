"""Bounded retry policy shared by downloads and online synthesis."""
import email.utils
import http.client
import queue
import threading
import time
import urllib.error
import urllib.request
from tts_config import byte_limit, network_timeout


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


def open_url(request, timeout):
    """Bound waiting for DNS, connection and headers, before reading any body.

    Python's socket timeout does not bound libc DNS or trickled HTTP headers.
    A daemon opener prevents those operations holding the caller indefinitely.
    A late response is closed without handing it to a decoder/model validator.
    """
    result, lock = queue.Queue(maxsize=1), threading.Lock()
    abandoned = threading.Event()

    def open_request():
        try:
            item = (True, urllib.request.urlopen(request, timeout=timeout))
        except Exception as error:
            item = (False, error)
        with lock:
            if not abandoned.is_set():
                result.put_nowait(item)
                return
        if item[0]:
            item[1].close()

    threading.Thread(target=open_request, daemon=True, name='tts-http-open').start()
    try:
        success, value = result.get(timeout=timeout)
    except queue.Empty as error:
        with lock:
            abandoned.set()
            try:
                success, value = result.get_nowait()
            except queue.Empty:
                success, value = False, None
        if success:
            value.close()
        raise TimeoutError('HTTP request exceeded its opening deadline.') from error
    if not success:
        raise value
    return value


def response_blocks(response, maximum, deadline):
    """Bound declared and actual bytes, with a deadline for the whole body."""
    length = getattr(response, 'headers', {}).get('Content-Length')
    if length is not None:
        try:
            length = int(length)
        except (TypeError, ValueError) as error:
            raise ValueError('Invalid HTTP Content-Length.') from error
        if length < 0 or length > maximum:
            raise ValueError(f'HTTP response exceeds byte limit ({maximum} bytes).')
    received = 0
    # read1 performs one underlying read; read(n) could stay alive indefinitely
    # when a peer sends a trickle faster than the per-operation socket timeout.
    read = response.read1 if isinstance(response, http.client.HTTPResponse) else response.read
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP response exceeded its total deadline.')
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        block = read(min(64 * 1024, maximum - received + 1))
        if time.monotonic() > deadline:
            raise TimeoutError('HTTP response exceeded its total deadline.')
        if not block:
            break
        received += len(block)
        if received > maximum:
            raise ValueError(f'HTTP response exceeds byte limit ({maximum} bytes).')
        yield block
    if length is not None and received != length:
        raise ConnectionError('Incomplete HTTP response.')


def read_url(request, timeout=None, max_bytes=None):
    timeout = timeout or network_timeout()
    maximum = max_bytes or byte_limit('TTS_MAX_RPC_BYTES', 8 * 1024 * 1024)
    def receive():
        deadline = time.monotonic() + timeout
        with open_url(request, timeout=timeout) as response:
            return b''.join(response_blocks(response, maximum, deadline))
    return retry(receive)
