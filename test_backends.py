"""Offline tests: Edge transport is simulated; Silero PCM uses real torch."""
import importlib.util
import asyncio
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import wave

import tts_engines as engines
import urllib.request


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        sleep = patch("tts_network.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    @unittest.skipUnless(importlib.util.find_spec('edge_tts'), 'edge_tts optional dependency is required')
    def test_edge_both_voices_and_audio_replacement(self):
        import edge_tts
        calls = []

        class Transport:
            def __init__(self, text, voice):
                calls.append((text, voice))

            async def stream(self):
                yield {'type': 'audio', 'data': b"synthetic MP3 transport data" * 4}

        with patch.object(edge_tts, "Communicate", Transport):
            for mode, voice in ((3, "ru-RU-SvetlanaNeural"), (4, "ru-RU-DmitryNeural")):
                synth = engines.Synthesizer(mode, "ru")
                audio = synth.generate("Первый текст.", self.directory)
                self.assertEqual(audio.suffix, ".mp3")
                audio.write_bytes(b"old" * 500)
                synth.generate("Второй текст.", self.directory)
                self.assertEqual(audio.read_bytes(), b"synthetic MP3 transport data" * 4)
                self.assertEqual(calls[-2:], [("Первый текст.", voice), ("Второй текст.", voice)])

    @unittest.skipUnless(importlib.util.find_spec('edge_tts'), 'edge_tts optional dependency is required')
    def test_edge_transport_failure_is_not_success(self):
        import edge_tts
        transport = MagicMock()
        async def fail():
            raise ConnectionError('connection failed')
            yield
        transport.stream = fail
        with patch.object(edge_tts, "Communicate", return_value=transport):
            synth = engines.Synthesizer(3, "ru")
            with self.assertRaisesRegex(ConnectionError, "connection failed"):
                synth.generate("Текст.", self.directory)

    @unittest.skipUnless(importlib.util.find_spec('edge_tts'), 'edge_tts optional dependency is required')
    def test_edge_empty_response_is_rejected(self):
        import edge_tts
        transport = MagicMock()
        async def empty():
            if False:
                yield
        transport.stream = empty
        with patch.object(edge_tts, "Communicate", return_value=transport):
            synth = engines.Synthesizer(4, "ru")
            with self.assertRaisesRegex(RuntimeError, "no audio was generated"):
                synth.generate("Текст.", self.directory)

    @unittest.skipUnless(importlib.util.find_spec('edge_tts'), 'edge_tts optional dependency is required')
    def test_edge_hanging_transport_times_out(self):
        import edge_tts
        transport = MagicMock()
        async def hang():
            await asyncio.Event().wait()
            yield
        transport.stream = hang
        with patch.object(edge_tts, "Communicate", return_value=transport), \
             patch.dict(os.environ, {"TTS_NETWORK_TIMEOUT": "0.02"}):
            synth = engines.Synthesizer(3, "ru")
            with self.assertRaises(TimeoutError):
                synth.generate("Текст.", self.directory)

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_both_voices_and_pcm_clamping(self):
        import torch
        model = MagicMock()
        def infer(**kwargs):
            self.assertTrue(torch.is_inference_mode_enabled())
            return torch.tensor([-2.0, -0.5, 0, 0.5, 2.0])
        model.apply_tts.side_effect = infer
        with patch.object(engines, "load_silero", return_value=model) as load:
            for mode, voice in ((5, "xenia"), (6, "aidar")):
                synth = engines.Synthesizer(mode, "ru")
                for text in ("Первый текст.", "Второй текст."):
                    audio = synth.generate(text, self.directory)
                    with wave.open(str(audio)) as wav:
                        self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
                        self.assertEqual(wav.getnframes(), 5)
                        import struct
                        self.assertEqual(struct.unpack("<5h", wav.readframes(5)), (-32767, -16383, 0, 16383, 32767))
                    model.apply_tts.assert_called_with(text=text, speaker=voice, sample_rate=24000)
            self.assertEqual(load.call_count, 2)  # One load per instance, not per chunk.

    def test_silero_rejects_unsupported_languages_before_loading(self):
        with patch.object(engines, "load_silero") as load, \
             patch.object(engines.tts_voices, "extra_silero_model", side_effect=ValueError("Silero has no model for 'zz'")):
            for mode in (5, 6):
                with self.assertRaisesRegex(ValueError, "no model for 'zz'"):
                    engines.Synthesizer(mode, "zz")
            load.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_inference_failure_is_not_silent_audio(self):
        model = MagicMock()
        model.apply_tts.side_effect = ValueError("model error")
        with patch.object(engines, "load_silero", return_value=model):
            synth = engines.Synthesizer(5, "ru")
            with self.assertRaisesRegex(ValueError, "model error"):
                synth.generate("Текст.", self.directory)
        self.assertFalse((self.directory / "speech.wav").exists())

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_model_download_and_cache_reuse(self):
        import torch
        payload = b"test package contents"
        with patch.dict(os.environ, {"TTS_CACHE_DIR": str(self.directory)}), \
             patch.object(urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download, \
             patch.object(torch.package, "PackageImporter") as importer, \
             patch.dict(engines.tts_voices.SILERO_PINS['v4_ru'], {'size_bytes': len(payload), 'sha256_digest': hashlib.sha256(payload).hexdigest()}):
            engines.load_silero(engines.tts_voices.silero_voice("ru", "female"))
            self.assertEqual((engines.silero_path(engines.tts_voices.silero_voice("ru", "female"))).read_bytes(), payload)
            self.assertFalse(list(self.directory.glob("*.part")))
            engines.load_silero(engines.tts_voices.silero_voice("ru", "female"))
            download.assert_called_once()
            self.assertEqual(download.call_args.args[0], "https://models.silero.ai/models/tts/ru/v4_ru.pt")
            self.assertEqual(importer.call_count, 2)

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_failed_download_leaves_no_cached_package(self):
        with patch.dict(os.environ, {"TTS_CACHE_DIR": str(self.directory)}), \
             patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.URLError):
                engines.load_silero(engines.tts_voices.silero_voice("ru", "female"))
        self.assertEqual(list(self.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
