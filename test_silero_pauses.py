"""Quiet-pause cleanup must preserve speech bytes and sample positions."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import tts_worker
import tts_engines
from tts_state import PCMCache


@unittest.skipUnless(importlib.util.find_spec('numpy'), 'numpy optional dependency is required')
class SileroPauseTests(unittest.TestCase):
    def fixture(self, gap_seconds=1.3, amplitude=50):
        import numpy as np
        rate = 24000
        voice = (3000 * np.sin(np.arange(rate) * 2 * np.pi * 240 / rate)).astype('<i2')
        quiet = np.random.default_rng(0).integers(-amplitude, amplitude + 1,
                                                  int(gap_seconds * rate), dtype=np.int16)
        return voice.tobytes() + quiet.astype('<i2').tobytes() + voice.tobytes()

    def test_only_long_quiet_pause_changes_without_time_shift(self):
        import numpy as np
        pcm = self.fixture()
        clean = tts_worker.clean_silero_pauses(pcm)
        self.assertEqual(len(clean), len(pcm))
        self.assertEqual(clean[:48000], pcm[:48000])
        self.assertEqual(clean[-48000:], pcm[-48000:])
        samples = np.frombuffer(clean, '<i2')
        self.assertFalse(samples[int(1.04 * 24000):int(2.26 * 24000)].any())
        self.assertEqual(tts_worker.clean_silero_pauses(clean), clean)

    def test_short_quiet_phonemes_and_normal_voiced_audio_are_preserved(self):
        for pcm in (self.fixture(.3), self.fixture(1.3, amplitude=1000)):
            self.assertEqual(tts_worker.clean_silero_pauses(pcm), pcm)

    def test_existing_digital_silence_and_empty_audio_are_unchanged(self):
        for pcm in (self.fixture(amplitude=0), b''):
            self.assertIs(tts_worker.clean_silero_pauses(pcm), pcm)

    def test_cleanup_rejects_incomplete_sample(self):
        with self.assertRaises(ValueError):
            tts_worker.clean_silero_pauses(b'\0')

    def test_model_pause_cleans_louder_noise_but_keeps_quiet_words(self):
        pcm = self.fixture(.65, amplitude=550)
        self.assertEqual(tts_worker.clean_silero_pauses(pcm), pcm)
        clean = tts_worker.clean_silero_pauses(pcm, [(1, 1.65)])
        self.assertNotEqual(clean, pcm)
        self.assertEqual(len(clean), len(pcm))
        self.assertEqual(clean[:48000], pcm[:48000])
        self.assertEqual(clean[-48000:], pcm[-48000:])
        self.assertEqual(tts_worker.clean_silero_pauses(clean, [(1, 1.65)]), clean)
        # The same quiet signal labelled as a word is never muted.
        self.assertEqual(tts_worker.clean_silero_pauses(pcm, []), pcm)
        # A generous alignment interval cannot erase normal-amplitude speech.
        self.assertEqual(tts_worker.clean_silero_pauses(self.fixture(amplitude=3000), [(0, 3.3)]),
                         self.fixture(amplitude=3000))

    def test_model_pause_rejects_invalid_intervals(self):
        for spans in [[(-1, 2)], [(2, 1)], [(0, float('nan'))]]:
            with self.assertRaises(ValueError):
                tts_worker.clean_silero_pauses(self.fixture(), spans)

    def test_old_leading_pause_is_cleaned_without_erasing_entire_quiet_recording(self):
        pcm = self.fixture()
        leading = pcm[48000:]
        cleaned = tts_worker.clean_silero_pauses(leading)
        self.assertNotEqual(cleaned, leading)
        self.assertEqual(cleaned[-48000:], leading[-48000:])
        self.assertEqual(tts_worker.clean_silero_pauses(cleaned), cleaned)
        all_quiet = pcm[48000:-48000]
        self.assertEqual(tts_worker.clean_silero_pauses(all_quiet), all_quiet)

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_alignment_merges_nonword_tokens_and_validates_lengths(self):
        import torch
        model = SimpleNamespace(window=.0125)
        spans = tts_engines.silero_pause_spans(model, [], torch.tensor([8, 4, 6, 2, 3]),
                                              [False, False, True, False, False])
        self.assertEqual(len(spans), 2)
        for actual, expected in zip(spans, [(0, .15), (.225, .2875)]):
            for value, target in zip(actual, expected):
                self.assertAlmostEqual(value, target)
        with self.assertRaises(ValueError):
            tts_engines.silero_pause_spans(model, [], torch.tensor([1]), [])
        with self.assertRaises(ValueError):
            tts_engines.silero_pause_spans(model, [], torch.tensor([-1]), [False])

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_dialogue_dash_normalization_keeps_words_and_other_punctuation(self):
        import torch
        from unittest.mock import Mock
        model = Mock()
        original_formatter = model.get_word_ts
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(tts_engines, 'load_silero', return_value=model):
            root = Path(temporary)
            for mode, text, expected in (
                (5, '— В глубине района.', 'В глубине района.'),
                (5, '– «Прямая речь».', '«Прямая речь».'),
                (5, 'Слово — ответ.', 'Слово — ответ.'),
                (5, '-10 градусов.', '-10 градусов.'),
                (6, '— В глубине района.', '— В глубине района.'),
            ):
                synth = tts_engines.Synthesizer(mode, 'ru')
                model.apply_tts.return_value = (torch.zeros(3000), []) if mode == 5 else torch.zeros(3000)
                synth.generate(text, root)
                self.assertEqual(model.apply_tts.call_args.kwargs['text'], expected)
                self.assertIs(model.get_word_ts, original_formatter)

    def test_worker_regenerates_legacy_cache_once_and_keeps_bookmark_timing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'book.txt'
            source.write_text('Короткий текст.')
            synth = SimpleNamespace(engine='Silero', language='ru', voice='xenia',
                                    spec={'model': 'v4_ru'}, pause_spans=[(1, 2.3)])
            job = {'text_file': str(source), 'mode': 5, 'language': 'ru',
                   'bookmark': {'seconds': 0, 'voice': ''}}
            pcm = self.fixture()
            synth.generate = lambda *args: root / 'speech.wav'
            class Accentor:
                identity = {'fixture': True}
                def __call__(self, text):
                    return text
            with patch.dict(os.environ, {'TTS_CACHE_DIR': str(root / 'cache')}), \
                 patch.object(tts_worker, 'Synthesizer', return_value=synth), \
                 patch.object(tts_worker, 'voice_identity', return_value='voice'), \
                 patch('tts_pronunciation.SileroPronunciation', return_value=Accentor()):
                identity = tts_worker.digest(['voice', Accentor.identity])
                key = tts_worker.digest([identity, source.read_text(), None])
                cache = PCMCache()
                cache.put(key, pcm)
                output = io.BytesIO()
                with patch.object(tts_worker, 'decode_pcm', return_value=pcm) as decode:
                    tts_worker.produce(job, root, output)
                decode.assert_called_once()
                self.assertEqual(output.getvalue(), tts_worker.clean_silero_pauses(pcm, synth.pause_spans))
                self.assertEqual(cache.get(key), output.getvalue())
                self.assertEqual(json.loads((root / 'voice.json').read_text())['voice'], identity)
                # A bookmark referencing the uncleaned bytes keeps its sample
                # offset because cleanup never changes the fragment's timing.
                job['bookmark'] = {'seconds': 1.65, 'voice': identity, 'cursor': {
                    'index': 0, 'fraction': .5,
                    'layout': tts_worker.digest([[source.read_text()], 'text-fragments-v1']),
                    'audio': hashlib.sha256(pcm).hexdigest()}}
                resumed = io.BytesIO()
                with patch.object(tts_worker, 'decode_pcm') as decode:
                    tts_worker.produce(job, root, resumed)
                decode.assert_not_called()
                clean = tts_worker.clean_silero_pauses(pcm, synth.pause_spans)
                self.assertEqual(resumed.getvalue(), clean[len(clean) // 2:])
                # The previous release may already have applied its quieter
                # amplitude-only cleanup. Its saved fingerprint also survives.
                legacy = tts_worker.clean_silero_pauses(pcm)
                cache.put(key, legacy)
                job['bookmark']['cursor']['audio'] = hashlib.sha256(legacy).hexdigest()
                resumed = io.BytesIO()
                with patch.object(tts_worker, 'decode_pcm', return_value=pcm):
                    tts_worker.produce(job, root, resumed)
                self.assertEqual(resumed.getvalue(), clean[len(clean) // 2:])
                # Different synthesized bytes have no proven timing relation:
                # replay the fragment instead of skipping possible words.
                cache.put(key, legacy)
                changed = pcm + b'\0\0' * 2400
                resumed = io.BytesIO()
                with patch.object(tts_worker, 'decode_pcm', return_value=changed):
                    tts_worker.produce(job, root, resumed)
                self.assertEqual(resumed.getvalue(), tts_worker.clean_silero_pauses(changed, synth.pause_spans))


if __name__ == '__main__':
    unittest.main()
