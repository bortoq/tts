"""Atomic PCM cache and playback checkpoints."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from tts_voices import cache_dir


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
        self.used = sum(p.stat().st_size for p in self.directory.glob('*.pcm'))
        if self.limit < 0:
            raise ValueError('TTS_PCM_CACHE_MB must not be negative.')

    def get(self, key):
        path = self.directory / (key + '.pcm')
        try:
            metadata = json.loads(path.with_suffix('.json').read_text())
            data = path.read_bytes()
            if not data or len(data) % 2 or metadata != {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}:
                raise ValueError('Invalid cached PCM')
            path.touch()
            return data
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            path.with_suffix('.json').unlink(missing_ok=True)
            return None

    def put(self, key, data):
        if not data or len(data) % 2:
            raise ValueError('PCM must contain complete 16-bit samples.')
        if not self.limit or len(data) > self.limit:
            return
        path = self.directory / (key + '.pcm')
        previous = path.stat().st_size if path.exists() else 0
        atomic_write(path, data)
        self.used += len(data) - previous
        atomic_write(path.with_suffix('.json'), json.dumps({'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode())
        if self.used <= self.limit:
            return
        files = sorted(self.directory.glob('*.pcm'), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for old in files:
            if total <= self.limit * 0.9:
                break
            total -= old.stat().st_size
            old.unlink(missing_ok=True)
            old.with_suffix('.json').unlink(missing_ok=True)
        self.used = total


class Bookmark:
    def __init__(self, text, mode, language):
        self.path = cache_dir() / 'positions' / (digest([text, mode, language]) + '.json')

    def read(self):
        try:
            value = json.loads(self.path.read_text())
            position = float(value['seconds'])
            if position >= 0 and position < float('inf') and isinstance(value['voice'], str):
                return value
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {'seconds': 0, 'voice': ''}

    def save(self, seconds, voice):
        atomic_write(self.path, json.dumps({'seconds': seconds, 'voice': voice}).encode())

    def clear(self):
        self.path.unlink(missing_ok=True)
