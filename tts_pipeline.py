"""Duration-based buffering, cancellation, and resumable playback."""
from bisect import bisect_right
from collections import deque
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from tts_playback import MpvPlayer
from tts_state import Bookmark, atomic_write
from tts_config import BYTES_PER_SECOND, MODES
from tts_voices import cache_dir, configured_voice, SILERO_TRUST_WARNING


def buffer_seconds(speed):
    seconds = float(os.environ.get('TTS_BUFFER_SECONDS', '8'))
    if not 0 < seconds <= 120:
        raise ValueError('TTS_BUFFER_SECONDS must be greater than 0 and at most 120.')
    return seconds * speed


class StartupIndicator:
    """Show preparation progress until the first audio is ready for mpv."""
    def __init__(self, directory, mode, speed):
        self.directory, self.speed = directory, speed
        self.engine, _, self.voice = MODES[mode]
        self.stop = threading.Event()
        self.thread = None
        self.finished = False
        self.terminal = sys.stderr.isatty()

    def label(self):
        try:
            voice = json.loads((self.directory / 'voice.json').read_text())
            self.engine = voice.get('engine', self.engine)
            self.voice = voice.get('name', self.voice)
        except (OSError, ValueError):
            pass
        return f'{self.engine}: {self.voice}; {self.speed:g}x.'

    def _run(self):
        frames = ('|', '/', '-', '\\')
        index = 0
        label = self.label()
        print(label + ' ', end='', file=sys.stderr, flush=True)
        while not self.stop.is_set():
            updated = self.label()
            if updated != label:
                label = updated
                prefix = '\r' + label + ' '
            else:
                prefix = '\b' if index else ''
            print(prefix + frames[index % len(frames)] + '\x1b[K', end='',
                  file=sys.stderr, flush=True)
            index += 1
            self.stop.wait(0.12)

    def __enter__(self):
        if self.terminal:
            self.thread = threading.Thread(target=self._run, name='tts-startup')
            self.thread.start()
        else:
            print(self.label(), file=sys.stderr, flush=True)
        return self

    def finish(self):
        if self.finished:
            return
        self.finished = True
        self.stop.set()
        if self.thread:
            self.thread.join()
            print('\b \b', file=sys.stderr, flush=True)

    def __exit__(self, *args):
        self.finish()


class AudioWorker:
    """A bounded queue in audio seconds, backed by a cancellable process group."""
    def __init__(self, directory, target, command=None, speed=1.0):
        self.directory, self.target = directory, target
        self.speed, self.wall_target = speed, target / speed
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
                block = self.process.stdout.read(BYTES_PER_SECOND)
                if not block:
                    break
                with self.condition:
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
        while True:
            player.ensure_running()
            self.update_speed()
            with self.condition:
                if self.buffered >= self.target or self.done:
                    return
                self.condition.wait(0.1)

    def get(self, player):
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
        self.timeline_offset = 0
        self.segments = []
        self.segment_starts = []

    def checkpoint(self):
        try:
            voice = json.loads((self.directory / 'voice.json').read_text())
            try:
                position = json.loads((self.directory / 'mpv-position.json').read_text())
            except (OSError, ValueError):
                position = {}
            if self.completed_audio is None:
                seconds = max(0, float(position['seconds']))
            else:
                seconds = self.completed_audio
            cursor = None
            timeline = self.directory / 'segments.jsonl'
            if timeline.exists():
                if timeline.stat().st_size < self.timeline_offset:
                    self.timeline_offset = 0
                    self.segments.clear()
                    self.segment_starts.clear()
                with timeline.open('rb') as stream:
                    stream.seek(self.timeline_offset)
                    while True:
                        line = stream.readline()
                        if not line.endswith(b'\n'):
                            break  # Retry an incomplete append on the next checkpoint.
                        segment = json.loads(line)
                        self.segments.append(segment)
                        self.segment_starts.append(segment['start'])
                        self.timeline_offset = stream.tell()
                index = bisect_right(self.segment_starts, seconds) - 1
                if index >= 0:
                    segment = self.segments[index]
                    duration = segment['duration']
                    fraction = min(1, max(0, (seconds - segment['start'] + segment['cut']) / duration))
                    cursor = {'cursor': {'index': segment['index'], 'fraction': fraction,
                        'layout': segment['layout'], **({'audio': segment['audio']} if 'audio' in segment else {})}}
            self.bookmark.save(voice['offset'] + seconds, voice['voice'], position.get('speed'), position, **({'cursor': cursor} if cursor else {}))
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
    engine, gender, _ = MODES[mode]
    if engine == 'Silero':
        custom = configured_voice('silero', language, gender)
        if custom and not custom.get('sha256_digest'):
            # Worker diagnostics go to a log. Show this explicit trust warning
            # in the terminal before the worker can execute a custom package.
            print(SILERO_TRUST_WARNING, file=sys.stderr)
    bookmark = Bookmark(text, mode, language)
    source = directory / 'book.txt'
    source.write_text(text, encoding='utf-8')
    resume = os.environ.get('TTS_RESUME', '1')
    if resume not in ('0', '1'):
        raise ValueError('TTS_RESUME must be 0 or 1.')
    position = bookmark.read() if resume == '1' else {'seconds': 0, 'voice': ''}
    speed = speed if speed is not None else position.get('speed', 1.0)
    atomic_write(directory / 'job.json', json.dumps({'text_file': str(source), 'mode': mode,
                 'language': language, 'bookmark': position, 'speed': speed}).encode())
    monitor = PositionMonitor(directory, bookmark)
    try:
        with StartupIndicator(directory, mode, speed) as startup, \
             MpvPlayer(directory, source=subprocess.PIPE, raw=True, speed=speed, state=position) as player:
            with monitor, AudioWorker(directory, buffer_seconds(speed), speed=speed) as worker:
                worker.prefill(player)
                block = worker.get(player)
                startup.finish()
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
        try:
            exit_state = json.loads((directory / 'mpv-exit.json').read_text())
        except (OSError, ValueError):
            exit_state = {}
        if exit_state.get('remember_position') is False:
            # Periodic checkpoints and mpv teardown may have saved the position.
            # Reset it only after both have finished, retaining player settings.
            saved = bookmark.read()
            bookmark.save(0, saved['voice'], saved.get('speed'), saved)
    bookmark.clear()
