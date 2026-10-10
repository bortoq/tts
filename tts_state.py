"""Atomic PCM cache and playback checkpoints."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import tempfile
from tts_voices import cache_dir
from tts_config import BYTES_PER_SECOND, byte_limit


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f'tts-{os.getpid()}-', delete=False, suffix='.part') as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class PCMCache:
    def __init__(self):
        self.directory = cache_dir() / 'pcm'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.limit = int(os.environ.get('TTS_PCM_CACHE_MB', '256')) * 1024 * 1024
        with self.locked():
            self.used = sum(p.stat().st_size for p in self.directory.glob('*.pcm'))
        if self.limit < 0:
            raise ValueError('TTS_PCM_CACHE_MB must not be negative.')

    @contextmanager
    def locked(self):
        with (self.directory / "cache.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def get(self, key):
        with self.locked():
            return self._get(key)

    def _access(self, key):
        # The file lock also serializes this logical clock across cache instances.
        # Filesystem timestamp resolution must not determine LRU ordering.
        with sqlite3.connect(self.directory / 'lru.sqlite3') as index:
            index.execute('CREATE TABLE IF NOT EXISTS access (key TEXT PRIMARY KEY, sequence INTEGER)')
            index.execute('INSERT OR REPLACE INTO access VALUES (?, '
                          '(SELECT COALESCE(MAX(sequence), 0) + 1 FROM access))', (key,))

    def _forget(self, key):
        if (self.directory / 'lru.sqlite3').exists():
            with sqlite3.connect(self.directory / 'lru.sqlite3') as index:
                index.execute('DELETE FROM access WHERE key = ?', (key,))

    def _get(self, key):
        path = self.directory / (key + '.pcm')
        try:
            metadata = json.loads(path.with_suffix('.json').read_text())
            maximum = byte_limit('TTS_MAX_PCM_BYTES', 180 * BYTES_PER_SECOND)
            size = path.stat().st_size
            if (not isinstance(metadata, dict) or type(metadata.get('bytes')) is not int
                    or metadata['bytes'] != size or not 0 < size <= maximum or size % 2):
                raise ValueError('Invalid or oversized cached PCM')
            with path.open('rb') as stream:
                data = stream.read(maximum + 1)
            if not data or len(data) % 2 or metadata != {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
                raise ValueError('Invalid cached PCM')
            self._access(key)
            return data
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            self._forget(key)
            return None

    def put(self, key, data):
        with self.locked():
            self.used = sum(p.stat().st_size for p in self.directory.glob("*.pcm"))
            return self._put(key, data)

    def _put(self, key, data):
        if not data or len(data) % 2:
            raise ValueError('PCM must contain complete 16-bit samples.')
        if not self.limit or len(data) > self.limit:
            return
        if len(data) > byte_limit('TTS_MAX_PCM_BYTES', 180 * BYTES_PER_SECOND):
            return
        path = self.directory / (key + '.pcm')
        previous = path.stat().st_size if path.exists() else 0
        atomic_write(path, data)
        self.used += len(data) - previous
        atomic_write(path.with_suffix('.json'), json.dumps({'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode())
        self._access(key)
        if self.used <= self.limit:
            return
        with sqlite3.connect(self.directory / 'lru.sqlite3') as index:
            order = dict(index.execute('SELECT key, sequence FROM access'))
        files = sorted((p for p in self.directory.glob('*.pcm') if p != path),
                       key=lambda p: (order.get(p.stem, 0), p.name))
        total = self.used
        for old in files:
            if total <= self.limit * 0.9:
                break
            total -= old.stat().st_size
            old.unlink(missing_ok=True)
            old.with_suffix('.json').unlink(missing_ok=True)
            self._forget(old.stem)
        self.used = total


class Bookmark:
    def __init__(self, text, mode, language):
        self.path = cache_dir() / 'positions' / (digest([text, mode, language]) + '.json')

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            position = float(value['seconds'])
            if position >= 0 and position < float('inf') and isinstance(value['voice'], str):
                speed = valid_speed(value.get('speed'))
                if speed is None:
                    value.pop('speed', None)
                else:
                    value['speed'] = speed
                return {**{'seconds': position, 'voice': value['voice']}, **player_state(value), **cursor_state(value)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {'seconds': 0, 'voice': ''}

    def save(self, seconds, voice, speed=None, state=None, cursor=None):
        value = {'seconds': seconds, 'voice': voice}
        speed = valid_speed(speed)
        if speed is not None:
            value['speed'] = speed
        value.update(player_state(state or {}))
        value.update(cursor_state(cursor or {}))
        atomic_write(self.path, json.dumps(value).encode())

    def clear(self):
        self.path.unlink(missing_ok=True)


def valid_speed(value):
    try:
        speed = float(value)
        return speed if math.isfinite(speed) and 0.01 <= speed <= 100 else None
    except (ValueError, TypeError):
        return None


def player_state(value):
    state = {}
    speed = valid_speed(value.get('speed'))
    if speed is not None:
        state['speed'] = speed
    volume = value.get('volume')
    if isinstance(volume, (int, float)) and math.isfinite(volume) and 0 <= volume <= 1000:
        state['volume'] = volume
    for key in ('mute', 'stats_visible'):
        if isinstance(value.get(key), bool):
            state[key] = value[key]
    if isinstance(value.get('osd_level'), int) and 0 <= value['osd_level'] <= 3:
        state['osd_level'] = value['osd_level']
    return state


def cursor_state(value):
    cursor = value.get('cursor')
    if not isinstance(cursor, dict):
        return {}
    index, fraction = cursor.get('index'), cursor.get('fraction')
    if type(index) is int and index >= 0 and isinstance(fraction, (int, float)) and math.isfinite(fraction) and 0 <= fraction <= 1 and isinstance(cursor.get('layout'), str):
        result = {'index': index, 'fraction': fraction, 'layout': cursor['layout']}
        if isinstance(cursor.get('audio'), str):
            result['audio'] = cursor['audio']
        return {'cursor': result}
    return {}
