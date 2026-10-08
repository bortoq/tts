"""Duration-based buffering, cancellation, and resumable playback."""
from collections import deque
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from tts_playback import MpvPlayer
from tts_state import Bookmark, atomic_write
from tts_config import BYTES_PER_SECOND
from tts_voices import cache_dir


def buffer_seconds(speed):
    seconds = float(os.environ.get('TTS_BUFFER_SECONDS', '8'))
    if not 0 < seconds <= 120:
        raise ValueError('TTS_BUFFER_SECONDS must be greater than 0 and at most 120.')
    return seconds * speed


class AudioWorker:
    """A bounded queue in audio seconds, backed by a cancellable process group."""
    def __init__(self, directory, target, command=None, speed=1.0):
        self.directory, self.target = directory, target
        self.speed, self.wall_target = speed, target / speed
        self.read_seconds, self.audio_seconds, self.rate_warned = 0, 0, False
        self.reader_error = None
        self.items, self.buffered = deque(), 0
        self.done, self.stopped = False, False
        self.condition = threading.Condition()
        self.command = command or [sys.executable, str(Path(__file__).with_name('tts_worker.py')), str(directory)]

    def __enter__(self):
        self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE, start_new_session=True)
        self.thread = threading.Thread(target=self._read, name='tts-pcm-reader')
        self.thread.start()
        return self

    def _read(self):
        try:
            while True:
                with self.condition:
                    while self.buffered >= self.target * 2 and not self.stopped:
                        self.condition.wait(0.1)
                    if self.stopped:
                        return
                started = time.monotonic()
                block = self.process.stdout.read(BYTES_PER_SECOND)
                if not block:
                    break
                with self.condition:
                    if self.audio_seconds:
                        self.read_seconds += time.monotonic() - started
                    self.audio_seconds += len(block) / BYTES_PER_SECOND
                    if not self.rate_warned and self.read_seconds > 2 and self.audio_seconds > 5:
                        rate = max(0, self.audio_seconds - 1) / self.read_seconds
                        if rate < self.speed:
                            print(f'Synthesis produces {rate:.2f} audio seconds per second; '
                                  f'playback speed is {self.speed:g}x.', file=sys.stderr)
                            self.rate_warned = True
                    self.items.append(block)
                    self.buffered += len(block) / BYTES_PER_SECOND
                    self.condition.notify_all()
        except Exception as error:
            self.reader_error = error
        finally:
            with self.condition:
                self.done = True
                self.condition.notify_all()

    def update_speed(self):
        try:
            value = json.loads((self.directory / 'mpv-position.json').read_text())
            speed = float(value['speed'])
            if not 0.01 <= speed <= 100:
                return
            with self.condition:
                self.speed, self.target = speed, self.wall_target * speed
                self.condition.notify_all()
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def prefill(self, player):
        start = time.monotonic()
        warned = False
        while True:
            player.ensure_running()
            self.update_speed()
            with self.condition:
                if self.buffered >= self.target or self.done:
                    return
                if not warned and time.monotonic() - start > self.wall_target * 2:
                    print('Synthesis is slower than playback; waiting for the audio buffer.', file=sys.stderr)
                    warned = True
                self.condition.wait(0.1)

    def get(self, player):
        waiting = time.monotonic()
        warned = False
        while True:
            player.ensure_running()
            self.update_speed()
            with self.condition:
                if self.items:
                    block = self.items.popleft()
                    self.buffered -= len(block) / BYTES_PER_SECOND
                    self.condition.notify_all()
                    return block
                if self.done:
                    return None
                if not warned and time.monotonic() - waiting > 1:
                    print('Waiting for synthesis; consider a lower reading speed.', file=sys.stderr)
                    warned = True
                self.condition.wait(0.1)

    def result(self):
        if self.reader_error:
            raise RuntimeError(f"Cannot read synthesis output: {self.reader_error}")
        if self.process.wait(timeout=5):
            path = self.directory / 'error.txt'
            raise RuntimeError(path.read_text() if path.exists() else 'Synthesis worker failed.')

    def __exit__(self, *args):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()
        # Kill the group even if the worker exited: descendants may still own pipes.
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        # A grandchild may ignore SIGTERM after its parent has already exited.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.thread.join(timeout=3)
        self.process.stdout.close()
        for partial in cache_dir().rglob(f'tts-{self.process.pid}-*.part'):
            partial.unlink(missing_ok=True)
        if self.thread.is_alive():
            raise RuntimeError('Synthesis reader did not stop.')


class PositionMonitor:
    def __init__(self, directory, bookmark):
        self.directory, self.bookmark = directory, bookmark
        self.stop = threading.Event()
        self.completed_audio = None

    def checkpoint(self):
        try:
            voice = json.loads((self.directory / 'voice.json').read_text())
            if self.completed_audio is None:
                position = json.loads((self.directory / 'mpv-position.json').read_text())
                seconds = max(0, float(position['seconds']))
            else:
                seconds = self.completed_audio
            self.bookmark.save(voice['offset'] + seconds, voice['voice'])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _run(self):
        while not self.stop.wait(0.5):
            self.checkpoint()

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, name='tts-bookmark')
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        self.checkpoint()


def read_aloud(path, mode, language, text, directory, speed):
    bookmark = Bookmark(text, mode, language)
    source = directory / 'book.txt'
    source.write_text(text, encoding='utf-8')
    resume = os.environ.get('TTS_RESUME', '1')
    if resume not in ('0', '1'):
        raise ValueError('TTS_RESUME must be 0 or 1.')
    position = bookmark.read() if resume == '1' else {'seconds': 0, 'voice': ''}
    atomic_write(directory / 'job.json', json.dumps({'text_file': str(source), 'mode': mode,
                 'language': language, 'bookmark': position}).encode())
    monitor = PositionMonitor(directory, bookmark)
    try:
        with MpvPlayer(directory, source=subprocess.PIPE, raw=True, speed=speed) as player:
            with monitor, AudioWorker(directory, buffer_seconds(speed), speed=speed) as worker:
                worker.prefill(player)
                block = worker.get(player)
                if block is None:
                    worker.result()
                else:
                    delivered = 0
                    while block is not None:
                        player.feed(block)
                        delivered += len(block)
                        block = worker.get(player)
                    player.finish()
                    player.wait()  # Drain already-generated audio before reporting a failure.
                    # time-pos can lag the last audio packet at EOF. Only after
                    # playback completes is all delivered PCM known to be played.
                    monitor.completed_audio = delivered / BYTES_PER_SECOND
                    worker.result()
    finally:
        monitor.checkpoint()
    bookmark.clear()
