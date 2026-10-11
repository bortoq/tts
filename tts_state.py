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
        if self.limit < 0:
            raise ValueError('TTS_PCM_CACHE_MB must not be negative.')
        with self.locked(), self._index() as index:
            self._trim(index)

    @contextmanager
    def locked(self):
        with (self.directory / "cache.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def _index(self):
        # TRUNCATE keeps the journal name stable: commits must not change the
        # directory timestamp used to detect legacy writers and interrupted I/O.
        index = sqlite3.connect(self.directory / 'lru.sqlite3')
        try:
            index.execute('PRAGMA journal_mode=TRUNCATE')
            with index:
                index.execute('CREATE TABLE IF NOT EXISTS access (key TEXT PRIMARY KEY, sequence INTEGER)')
                index.execute('CREATE TABLE IF NOT EXISTS sizes (key TEXT PRIMARY KEY, bytes INTEGER)')
                index.execute('CREATE TABLE IF NOT EXISTS accounting '
                              '(id INTEGER PRIMARY KEY, used INTEGER, sequence INTEGER, stamp TEXT)')
                # Also creates the persistent journal before recording a stamp.
                index.execute('INSERT OR IGNORE INTO accounting VALUES (1, 0, 0, NULL)')
                used, sequence, stamp = index.execute(
                    'SELECT used, sequence, stamp FROM accounting WHERE id=1').fetchone()
                self.used = used
                if stamp != self._stamp():
                    self._reconcile(index)
                yield index
                index.execute('UPDATE accounting SET used=?, stamp=? WHERE id=1',
                              (self.used, self._stamp()))
        finally:
            index.close()

    def _stamp(self):
        stat = self.directory.stat()
        return f'{stat.st_mtime_ns}:{stat.st_ctime_ns}'

    def _reconcile(self, index):
        """Scan only after out-of-band directory changes or an interrupted write."""
        files = [(p.stem, p.stat().st_size) for p in self.directory.glob('*.pcm')]
        index.execute('DELETE FROM sizes')
        index.executemany('INSERT INTO sizes VALUES (?, ?)', files)
        index.execute('DELETE FROM access WHERE key NOT IN (SELECT key FROM sizes)')
        index.execute('UPDATE accounting SET sequence=MAX(sequence, '
                      '(SELECT COALESCE(MAX(sequence), 0) FROM access)) WHERE id=1')
        self.used = sum(size for _, size in files)

    def get(self, key, *, with_processing=False):
        with self.locked(), self._index() as index:
            self._trim(index)
            return self._get(key, index, with_processing=with_processing)

    def _access(self, key, index):
        index.execute('UPDATE accounting SET sequence=sequence+1 WHERE id=1')
        index.execute('INSERT OR REPLACE INTO access VALUES (?, '
                      '(SELECT sequence FROM accounting WHERE id=1))', (key,))

    def _forget(self, key, index):
        row = index.execute('SELECT bytes FROM sizes WHERE key=?', (key,)).fetchone()
        if row:
            self.used -= row[0]
        index.execute('DELETE FROM sizes WHERE key=?', (key,))
        index.execute('DELETE FROM access WHERE key=?', (key,))

    def _get(self, key, index, *, with_processing=False):
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
            expected = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            if 'processing' in metadata:
                expected['processing'] = metadata['processing']
            if not data or len(data) % 2 or metadata != expected:
                raise ValueError('Invalid cached PCM')
            self._access(key, index)
            return (data, metadata.get('processing')) if with_processing else data
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            self._forget(key, index)
            return (None, None) if with_processing else None

    def put(self, key, data, *, processing=None):
        with self.locked(), self._index() as index:
            return self._put(key, data, index, processing=processing)

    def _put(self, key, data, index, *, processing=None):
        if not data or len(data) % 2:
            raise ValueError('PCM must contain complete 16-bit samples.')
        if not self.limit or len(data) > self.limit:
            return
        if len(data) > byte_limit('TTS_MAX_PCM_BYTES', 180 * BYTES_PER_SECOND):
            return
        path = self.directory / (key + '.pcm')
        row = index.execute('SELECT bytes FROM sizes WHERE key=?', (key,)).fetchone()
        previous = row[0] if row else 0
        atomic_write(path, data)
        metadata = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        if processing is not None:
            metadata['processing'] = processing
        atomic_write(path.with_suffix('.json'), json.dumps(metadata).encode())
        index.execute('INSERT OR REPLACE INTO sizes VALUES (?, ?)', (key, len(data)))
        self.used += len(data) - previous
        self._access(key, index)
        self._trim(index, exclude=key)

    def _trim(self, index, exclude=None):
        """Apply this client's budget using shared accounting, including on reads."""
        if self.used <= self.limit:
            return
        files = index.execute(
            'SELECT sizes.key, sizes.bytes FROM sizes LEFT JOIN access USING (key) '
            'WHERE sizes.key != ? ORDER BY COALESCE(sequence, 0), sizes.key',
            (exclude or '',)).fetchall()
        for key, size in files:
            if self.used <= self.limit * 0.9:
                break
            path = self.directory / (key + '.pcm')
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            self._forget(key, index)


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
