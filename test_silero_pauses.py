"""Pre-vocoder correction, text preservation and cache/bookmark migration."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import tts_engines
import tts_worker
from tts_silero import MEL_PAUSE_REVISION, SileroV4PauseAdapter, quiet_pause_mel
from tts_state import PCMCache, digest


@unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
class SileroMelPauseTests(unittest.TestCase):
    def fixture(self, pause=52, token=46):
        import torch
        durations = torch.tensor([[80., float(pause), 80.]])
        sequence = torch.tensor([[1, token, 2]])
        mel = torch.full((1, 128, 160 + pause), 10.)
        return mel, durations, [True, False, True], sequence, {46}, -6.5

    def test_long_punctuation_pause_changes_only_its_protected_middle(self):
        import torch
        args = self.fixture()
        clean = quiet_pause_mel(*args)
        self.assertEqual(clean.shape, args[0].shape)
        self.assertTrue(torch.equal(clean[:, :, :90], args[0][:, :, :90]))
        self.assertTrue(torch.equal(clean[:, :, 120:], args[0][:, :, 120:]))
        self.assertTrue(torch.all(clean[:, :, 92:118] == -6.5))
        self.assertTrue(torch.all(clean[:, :, 90:92] < 10))
        self.assertTrue(torch.all(args[0] == 10))  # Reference spectrum is untouched.

    def test_short_pauses_and_word_tokens_are_preserved(self):
        for args in [self.fixture(pause=20), self.fixture(token=3)]:
            self.assertIs(quiet_pause_mel(*args), args[0])
        args = list(self.fixture())
        args[2] = [True, True, True]
        self.assertIs(quiet_pause_mel(*args), args[0])

    def test_long_space_pause_is_corrected(self):
        import torch
        args = list(self.fixture(token=47))
        args[4] = {46, 47}
        self.assertTrue(torch.all(quiet_pause_mel(*args)[:, :, 92:118] == -6.5))

    def test_adjacent_nonword_tokens_and_leading_dash_form_one_pause(self):
        import torch
        mel = torch.ones((1, 128, 132))
        clean = quiet_pause_mel(mel, torch.tensor([[4., 48., 80.]]),
                                [False, False, True], torch.tensor([[0, 46, 1]]), {46}, -6.5)
        self.assertTrue(torch.all(clean[:, :, 12:38] == -6.5))
        self.assertTrue(torch.equal(clean[:, :, 40:], mel[:, :, 40:]))

    def test_bad_alignment_is_rejected(self):
        import torch
        for durations in [torch.tensor([[80., -1., 80.]]), torch.tensor([[80., 52.5, 80.]]),
                          torch.tensor([[80., float('nan'), 80.]]), torch.tensor([[1., 1., 1.]])]:
            args = list(self.fixture())
            args[1] = durations
            with self.assertRaises(ValueError):
                quiet_pause_mel(*args)
        args = list(self.fixture())
        args[2] = []
        with self.assertRaises(ValueError):
            quiet_pause_mel(*args)

    def test_adapter_passes_corrected_spectrum_to_vocoder_and_reuses_predictions(self):
        import torch
        mel, durations, mask, seq, _, silence = self.fixture()
        vocoder = Mock(side_effect=lambda value, *args: value.mean(dim=1, keepdim=True))
        tts = SimpleNamespace(dur_predictor=Mock(return_value=torch.log(durations + 1)),
                              pitch_predictor=Mock(return_value=torch.zeros((1, 1, 3))),
                              update_pitch_coef=lambda pitch, *args: pitch,
                              tacotron=Mock(return_value=mel), vocoder=vocoder, sil_value=silence)
        system = SimpleNamespace(_tokenize_clean=lambda *args: ('raw', 'tokens', 'mask'),
                                 _get_model_preds=lambda *args: (0, 0, 0, 0),
                                 postprocess_accentor=lambda *args: 'marked',
                                 _fuse_words_to_sentence=lambda text: text,
                                 merge_batch_model=lambda *args: (seq, {}, torch.ones_like(durations),
                                                                  torch.ones_like(durations), mask),
                                 tts_model=tts, symbol_to_id={'.': 46})
        model = SimpleNamespace(get_speakers=lambda speaker: ([0], 0),
                                prepare_tts_model_input=lambda *args, **kwargs:
                                (['sentence'], ['clean'], [None], [1.], [1.], torch.tensor([0])),
                                symb_to_ascii=lambda symbol: symbol, q_model_unpacked=False, unpack_q_model=Mock(), models=[system])
        adapter = SileroV4PauseAdapter(model)
        audio, reference = adapter.generate('— Текст.', 'xenia', 24000, reference=True)
        self.assertTrue(torch.all(audio[92:118] == silence))
        self.assertTrue(torch.all(reference == 10))
        self.assertEqual(vocoder.call_count, 2)
        tts.tacotron.assert_called_once()
        tts.dur_predictor.assert_called_once()
        model.unpack_q_model.assert_called_once()
        vocoder.reset_mock()
        adapter.generate('Текст.', 'xenia', 24000)
        vocoder.assert_called_once()  # Normal synthesis does not render reference PCM.
        model.unpack_q_model.assert_called_once()

    def test_synthesizer_preserves_dialogue_punctuation(self):
        import torch
        model = Mock()
        adapter = Mock()
        adapter.generate.return_value = (torch.zeros(3000), None)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(tts_engines, 'load_silero', return_value=model), \
             patch.object(tts_engines, 'SileroV4PauseAdapter', return_value=adapter):
            synth = tts_engines.Synthesizer(5, 'ru')
            self.assertEqual(synth.processing_revision, MEL_PAUSE_REVISION)
            for text in ['— В глубине района.', '– «Прямая речь».', 'Слово — ответ.', '-10 градусов.']:
                synth.generate(text, Path(temporary))
                adapter.generate.assert_called_with(text, 'xenia', 24000, reference=False)
            model.apply_tts.assert_not_called()

    def test_adapter_is_not_applied_to_unreviewed_package_bytes(self):
        import torch
        spec = tts_engines.tts_voices.silero_voice('ru', 'female').copy()
        spec['sha256_digest'] = '0' * 64
        model = Mock()
        model.apply_tts.return_value = torch.zeros(3000)
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(tts_engines, 'load_silero', return_value=model), \
             patch.object(tts_engines.tts_voices, 'silero_voice', return_value=spec), \
             patch.object(tts_engines, 'SileroV4PauseAdapter') as adapter:
            synth = tts_engines.Synthesizer(5, 'ru')
            synth.generate('— Текст.', Path(temporary))
            self.assertIsNone(synth.processing_revision)
            adapter.assert_not_called()
            model.apply_tts.assert_called_once_with(text='— Текст.', speaker='xenia', sample_rate=24000)


class SileroMigrationTests(unittest.TestCase):
    def test_old_cache_migrates_once_and_bookmark_aliases_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'book.txt'
            source.write_text('Короткий текст.')
            corrected, raw, legacy = b'\1\0' * 24000, b'\2\0' * 24000, b'\3\0' * 24000
            synth = SimpleNamespace(engine='Silero', language='ru', voice='xenia', spec={'model': 'v4_ru'},
                                    processing_revision=MEL_PAUSE_REVISION, reference_file=root / 'reference.wav')
            synth.generate = Mock(return_value=root / 'speech.wav')
            class Accentor:
                identity = {'fixture': True}
                def __call__(self, text):
                    return text
            with patch.dict(os.environ, {'TTS_CACHE_DIR': str(root / 'cache')}), \
                 patch.object(tts_worker, 'Synthesizer', return_value=synth), \
                 patch.object(tts_worker, 'voice_identity', return_value='voice'), \
                 patch('tts_pronunciation.SileroPronunciation', return_value=Accentor()):
                identity = digest(['voice', Accentor.identity])
                key = digest([identity, source.read_text(), None])
                cache = PCMCache()
                cache.put(key, legacy, processing={'revision': 'silero-pause-mask-v1',
                                                 'compatible_audio': [hashlib.sha256(raw).hexdigest()]})
                job = {'text_file': str(source), 'mode': 5, 'language': 'ru',
                       'bookmark': {'seconds': .5, 'voice': identity, 'cursor': {
                           'index': 0, 'fraction': .5,
                           'layout': digest([[source.read_text()], 'text-fragments-v1']),
                           'audio': hashlib.sha256(legacy).hexdigest()}}}
                output = io.BytesIO()
                with patch.object(tts_worker, 'decode_pcm', side_effect=[corrected, raw]) as decode:
                    tts_worker.produce(job, root, output)
                self.assertEqual(decode.call_count, 2)
                self.assertTrue(synth.reference_requested)
                self.assertEqual(output.getvalue(), corrected[24000:])
                self.assertEqual(cache.get(key), corrected)
                self.assertEqual(json.loads((root / 'voice.json').read_text())['voice'], identity)
                with patch.object(tts_worker, 'decode_pcm') as decode:
                    output = io.BytesIO()
                    tts_worker.produce(job, root, output)
                decode.assert_not_called()
                self.assertEqual(output.getvalue(), corrected[24000:])
                # Unproven timing, including the former removed dialogue dash,
                # replays the fragment. No old cleanup algorithm is needed.
                cache.put(key, legacy)
                with patch.object(tts_worker, 'decode_pcm', side_effect=[corrected, raw]):
                    output = io.BytesIO()
                    tts_worker.produce(job, root, output)
                self.assertEqual(output.getvalue(), corrected)


if __name__ == '__main__':
    unittest.main()
