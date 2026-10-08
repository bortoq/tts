#!/usr/bin/env python3
"""Real synthesis tests: python3 integration_tests.py --engine rhvoice|edge|silero|piper|google|all.

Requires network for Edge and the first Silero download. Never mocks synthesis
and never treats unavailable dependencies/network as a successful or skipped test.
"""
import argparse
import array
import math
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import tts_config as config
import tts_engines as engines


TEXT = "Проверка чтения вслух. Сегодня прекрасный день. Один, два, три."


class RealSynthesis(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tts_integration_")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def assert_speech(self, audio):
        ffmpeg = config.executable("ffmpeg", None)
        decoded = subprocess.run(
            [ffmpeg, "-v", "error", "-i", str(audio), "-f", "s16le", "-ac", "1", "-ar", "24000", "pipe:1"],
            capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode(errors="replace"))
        pcm = array.array("h")
        pcm.frombytes(decoded.stdout)
        if sys.byteorder != "little":
            pcm.byteswap()
        self.assertGreater(len(pcm), 6000, "Less than 0.25 seconds of audio")
        rms = math.sqrt(sum(sample * sample for sample in pcm) / len(pcm))
        self.assertGreater(rms, 10, "Silence instead of speech")
        self.assertGreater(max(pcm) - min(pcm), 100, "No audio signal")

    def check_mode(self, mode, language="ru"):
        # A process deadline also bounds DNS resolver threads during asyncio cleanup.
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(mode),
                   "--timeout", str(config.network_timeout()), "--language", language]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as process:
            try:
                output, errors = process.communicate(timeout=4 * config.network_timeout() + 15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, errors = process.communicate()
                self.fail(f'{config.MODES[mode][0]} exceeded its process deadline. ' + output + errors)
        self.assertEqual(process.returncode, 0, output + errors)

    def synthesize_mode(self, mode, language="ru"):
        synth = engines.Synthesizer(mode, language)
        if language == 'ru':
            self.assertEqual(synth.voice, "ru" if mode == 9 else config.MODES[mode][2])
        text = TEXT if language == 'ru' else 'This is a real speech test. Today is a beautiful day.'
        audio = synth.generate(text, self.directory)
        self.assert_speech(audio)


class EdgeTests(RealSynthesis):
    def test_female_real_synthesis(self):
        self.check_mode(3)

    def test_male_real_synthesis(self):
        self.check_mode(4)


class SileroTests(RealSynthesis):
    def test_female_real_synthesis(self):
        self.check_mode(5)

    def test_male_real_synthesis(self):
        self.check_mode(6)


class RHVoiceTests(RealSynthesis):
    def test_female_real_synthesis(self):
        self.check_mode(1)

    def test_male_real_synthesis(self):
        self.check_mode(2)

    def test_english_real_synthesis(self):
        self.check_mode(1, 'en-US')


class PiperTests(RealSynthesis):
    def test_female_real_synthesis(self):
        self.check_mode(7)

    def test_male_real_synthesis(self):
        self.check_mode(8)


class GoogleTests(RealSynthesis):
    def test_mode_9_real_synthesis(self):
        self.check_mode(9)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("rhvoice", "edge", "silero", "piper", "google", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=30, help="network timeout in seconds")
    parser.add_argument("--worker", type=int, choices=range(1, 10), help=argparse.SUPPRESS)
    parser.add_argument("--language", default="ru", help="language for worker synthesis")
    args = parser.parse_args()
    if not 0 < args.timeout <= 3600:
        parser.error("timeout must be greater than 0 and at most 3600")
    os.environ["TTS_NETWORK_TIMEOUT"] = str(args.timeout)
    os.environ.setdefault("TTS_CACHE_DIR", str(Path(__file__).resolve().parent / ".cache"))
    if args.worker is not None:
        test = RealSynthesis()
        test.setUp()
        try:
            test.synthesize_mode(args.worker, args.language)
        finally:
            test.doCleanups()
        return 0
    suite = unittest.TestSuite()
    for name, tests in (("rhvoice", RHVoiceTests), ("edge", EdgeTests), ("silero", SileroTests), ("piper", PiperTests), ("google", GoogleTests)):
        if args.engine in (name, "all"):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(tests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
