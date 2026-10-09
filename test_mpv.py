"""Exercise actual terminal keys in one persistent mpv, with a silent output."""
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import select
import sys
import tempfile
import time
import unittest
import wave


@unittest.skipUnless(shutil.which("mpv"), "mpv is required")
class InteractiveMpvTests(unittest.TestCase):
    def test_continuous_stream_space_speed_and_q(self):
        with tempfile.TemporaryDirectory(prefix="tts_keys_") as temporary:
            directory = Path(temporary)
            for filename in ("first.wav", "second.wav"):
                with wave.open(str(directory / filename), "wb") as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(24000)
                    audio.writeframes(b"\0\0" * (24000 * 4))
            code = '''
import sys
import subprocess
import threading
import wave
from pathlib import Path
from unittest.mock import patch
import tts_playback as tts
directory = Path(sys.argv[1])
command = tts.player_command()
command = command[:-1] + ["--no-config", "--ao=null", "--"]
try:
    with patch.object(tts, "player_command", return_value=command):
        with tts.MpvPlayer(directory, source=subprocess.PIPE, raw=True, speed=2.3) as player:
            def feed():
                try:
                    for filename in ('first.wav', 'second.wav'):
                        with wave.open(str(directory / filename)) as audio:
                            player.feed(audio.readframes(audio.getnframes()))
                    (directory / 'next').touch()
                    player.finish()
                except tts.PlaybackStopped:
                    pass
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            player.wait()
except tts.PlaybackStopped:
    pass
'''
            pid, terminal = pty.fork()
            if pid == 0:
                os.execv(sys.executable, [sys.executable, "-c", code, str(directory)])
            reaped = False
            try:
                requests = 0
                def property_value(name):
                    nonlocal requests
                    requests += 1
                    events = directory / "mpv-events.jsonl"
                    offset = events.stat().st_size
                    request = {"command": ["get_property", name], "request_id": 100 + requests}
                    with (directory / "mpv-commands.jsonl").open("a") as commands:
                        commands.write(json.dumps(request) + "\n")
                    with events.open() as lines:
                        lines.seek(offset)
                        deadline = time.monotonic() + 2
                        while time.monotonic() < deadline:
                            line = lines.readline()
                            if line:
                                response = json.loads(line)
                                if response.get("request_id") == request["request_id"]:
                                    return response.get("data")
                            else:
                                time.sleep(0.02)
                        output = b""
                        while select.select([terminal], [], [], 0)[0]:
                            try:
                                output += os.read(terminal, 65536)
                            except OSError:
                                break
                        self.fail("mpv did not respond to property query: " + output.decode(errors="replace"))

                def wait_for(predicate, label, timeout=8):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        try:
                            if predicate():
                                return
                        except (FileNotFoundError, ConnectionRefusedError):
                            pass
                        time.sleep(0.03)
                    self.fail(f"mpv did not {label}")

                wait_for(lambda: property_value("path") == "-" and property_value("time-pos") is not None, "start stream")
                self.assertAlmostEqual(property_value("speed"), 2.3)
                os.write(terminal, b" ")
                wait_for(lambda: property_value("pause") is True, "pause on Space")
                terminal_output = bytearray()
                def terminal_contains(text):
                    while select.select([terminal], [], [], 0)[0]:
                        try:
                            terminal_output.extend(os.read(terminal, 65536))
                        except OSError:
                            break
                    return text in terminal_output
                terminal_contains(b'Audio:')
                self.assertNotIn(b'Audio:', terminal_output)
                self.assertNotIn(b'Audio --aid=', terminal_output)
                self.assertNotIn(b'AO: [', terminal_output)
                self.assertNotIn(b'Reading from stdin', terminal_output)
                terminal_output.clear()
                os.write(terminal, b'\x09')  # Ctrl+I / Tab
                wait_for(lambda: terminal_contains(b'Audio:'), 'show information on Ctrl+I')
                self.assertIn(b'pcm_s16le', terminal_output)
                os.write(terminal, b'\x09')  # Close stats before checking the normal status line.
                terminal_output.clear()
                os.write(terminal, b'\x0f')  # Ctrl+O
                wait_for(lambda: property_value('osd-level') == 3, 'toggle OSD on Ctrl+O')
                try:
                    wait_for(lambda: terminal_contains(b'A:'), 'keep the native mpv status visible')
                except AssertionError:
                    self.fail('Native status output: ' + terminal_output.decode(errors='replace'))
                before = property_value("time-pos")
                time.sleep(0.2)
                self.assertAlmostEqual(property_value("time-pos"), before, delta=0.05)
                os.write(terminal, b"]")
                wait_for(lambda: property_value("speed") > 2.3, "increase speed on ]")
                speed = property_value("speed")
                os.write(terminal, b"[")
                wait_for(lambda: property_value("speed") < speed, "decrease speed on [")
                os.write(terminal, b"]")
                wait_for(lambda: property_value("speed") >= speed, "increase speed again")
                os.write(terminal, b" ")
                wait_for(lambda: property_value("pause") is False, "resume on Space")
                wait_for(lambda: property_value("time-pos") >= 4, "continue into second PCM block")
                self.assertTrue((directory / "next").is_file())
                events = [json.loads(line) for line in (directory / "mpv-events.jsonl").read_text().splitlines()]
                self.assertEqual(sum(event.get('event') == 'start-file' for event in events), 1)
                self.assertEqual(sum(event.get('event') == 'end-file' for event in events), 0)
                self.assertEqual(property_value("path"), "-")
                self.assertAlmostEqual(property_value("speed"), speed)
                os.write(terminal, b" ")
                wait_for(lambda: property_value("pause") is True, "pause second part")
                os.write(terminal, b"q")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        reaped = True
                        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                        break
                    time.sleep(0.03)
                else:
                    self.fail("q did not exit playback")
            finally:
                os.close(terminal)
                if not reaped:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
