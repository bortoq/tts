"""Real mpv/worker integration in a PTY, without using the speakers."""
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(shutil.which('mpv') and shutil.which('ffmpeg'), 'mpv and ffmpeg required')
class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='tts_pipeline_test_')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.pid = None
        self.terminal = None
        self.requests = 1000
        self.output = bytearray()
        self.addCleanup(self.stop)

    def stop(self):
        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(self.pid, 0)
            self.pid = None
        if self.terminal is not None:
            os.close(self.terminal)
            self.terminal = None

    def drain(self):
        while select.select([self.terminal], [], [], 0)[0]:
            try:
                self.output.extend(os.read(self.terminal, 65536))
            except OSError:
                break

    def wait_for(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
            time.sleep(0.02)
        self.fail('Timed out: ' + self.output.decode(errors='replace'))

    def launch(self, fake=False, complete=False, failure=False):
        code = '''import json, os, subprocess, sys
from pathlib import Path
from unittest.mock import patch
import tts_pipeline
import tts_playback
from tts_playback import PlaybackStopped
root = Path(sys.argv[1]); mode = sys.argv[2]; fake = mode == 'fake'
os.environ['TTS_CACHE_DIR'] = str(root / 'cache')
os.environ['TTS_BUFFER_SECONDS'] = '1'
command = tts_playback.player_command()
command = command[:-1] + ['--no-config', '--ao=null', '--term-status-msg=', '--']
text = 'Проверка плавного чтения. Сегодня прекрасный день. ' * (1 if mode == 'complete' else 80)
worker = tts_pipeline.AudioWorker
slow_code = "import pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(60)"
failure_code = "import pathlib,json,sys; root=pathlib.Path(sys.argv[1]); (root/'voice.json').write_text(json.dumps({'voice':'test','offset':0})); sys.stdout.buffer.write(bytes([1,0])*48000);sys.stdout.buffer.flush();(root/'error.txt').write_text('service unavailable');sys.exit(1)"
def create(directory, target, **kwargs):
    if mode == 'failure':
        return worker(directory, target, [sys.executable, '-c', failure_code, str(root)], **kwargs)
    return worker(directory, target, [sys.executable, '-c', slow_code, str(root / 'child.pid')]) if fake else worker(directory, target, **kwargs)
try:
    with patch.object(tts_playback, 'player_command', return_value=command), patch.object(tts_pipeline, 'AudioWorker', side_effect=create):
        tts_pipeline.read_aloud(root / 'book.txt', 1, 'ru', text, root, 2.3)
except PlaybackStopped:
    pass
except RuntimeError as error:
    if mode != 'failure' or str(error) != 'service unavailable':
        raise
    (root / 'expected-error').touch()
'''
        pid, terminal = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, '-c', code, str(self.directory), 'fake' if fake else 'failure' if failure else 'complete' if complete else 'native'])
        self.pid, self.terminal = pid, terminal

    def property(self, name):
        events = self.directory / 'mpv-events.jsonl'
        if not events.exists():
            return None
        offset = events.stat().st_size
        self.requests += 1
        request = {'command': ['get_property', name], 'request_id': self.requests}
        with (self.directory / 'mpv-commands.jsonl').open('a') as stream:
            stream.write(json.dumps(request) + '\n')
        with events.open() as stream:
            stream.seek(offset)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                self.drain()
                line = stream.readline()
                if line:
                    value = json.loads(line)
                    if value.get('request_id') == self.requests:
                        return value.get('data')
                else:
                    time.sleep(0.01)
        return None

    def quit(self):
        os.write(self.terminal, b'q')
        self.wait_exit()

    def wait_exit(self):
        status = None
        def exited():
            nonlocal status
            done, value = os.waitpid(self.pid, os.WNOHANG)
            if done:
                status = value
                self.pid = None
                return True
            return False
        self.wait_for(exited, timeout=5)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, self.output.decode(errors='replace'))
        self.drain()
        os.close(self.terminal)
        self.terminal = None

    def test_worker_failure_drains_audio_and_retains_bookmark(self):
        self.launch(failure=True)
        self.wait_exit()
        self.assertTrue((self.directory / 'expected-error').exists())
        checkpoints = list((self.directory / 'cache/positions').glob('*.json'))
        self.assertEqual(len(checkpoints), 1)
        self.assertAlmostEqual(json.loads(checkpoints[0].read_text())['seconds'], 2)
        events = [json.loads(line) for line in (self.directory / 'mpv-events.jsonl').read_text().splitlines()]
        self.assertTrue(any(event.get('reason') == 'eof' for event in events))

    @unittest.skipUnless(shutil.which('RHVoice-test'), 'RHVoice required')
    def test_complete_native_playback_clears_bookmark(self):
        self.launch(complete=True)
        self.wait_exit()
        self.assertFalse(list((self.directory / 'cache/positions').glob('*.json')))
        events = [json.loads(line) for line in (self.directory / 'mpv-events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(event.get('event') == 'start-file' for event in events), 1)
        self.assertTrue(any(event.get('reason') == 'eof' for event in events))

    def test_q_during_startup_download_cancels_worker_and_descendant(self):
        self.launch(fake=True)
        self.wait_for(lambda: (self.directory / 'child.pid').exists())
        child = int((self.directory / 'child.pid').read_text())
        os.write(self.terminal, b' ')
        self.wait_for(lambda: self.property('pause') is True)
        self.quit()
        status = Path(f'/proc/{child}/stat')
        if status.exists():
            self.assertEqual(status.read_text().split()[2], 'Z')

    @unittest.skipUnless(shutil.which('RHVoice-test'), 'RHVoice required')
    def test_native_stream_pause_quit_and_resume_uses_played_time(self):
        self.launch()
        self.wait_for(lambda: (self.property('time-pos') or 0) > 0.3)
        os.write(self.terminal, b' ')
        self.wait_for(lambda: self.property('pause') is True)
        played = self.property('time-pos')
        self.quit()
        checkpoints = list((self.directory / 'cache/positions').glob('*.json'))
        self.assertEqual(len(checkpoints), 1)
        checkpoint = json.loads(checkpoints[0].read_text())
        self.assertGreater(checkpoint['seconds'], 0)
        self.assertAlmostEqual(checkpoint['seconds'], played, delta=0.7)
        # Restart with the same text and mode: the worker validates voice identity.
        for name in ('mpv-events.jsonl', 'mpv-commands.jsonl', 'mpv-position.json', 'voice.json'):
            (self.directory / name).unlink(missing_ok=True)
        self.launch()
        self.wait_for(lambda: (self.directory / 'voice.json').exists())
        info = json.loads((self.directory / 'voice.json').read_text())
        self.assertAlmostEqual(info['offset'], checkpoint['seconds'])
        self.wait_for(lambda: (self.property('time-pos') or 0) > 0.2)
        self.quit()
        self.assertGreater(json.loads(checkpoints[0].read_text())['seconds'], checkpoint['seconds'])


if __name__ == '__main__':
    unittest.main()
