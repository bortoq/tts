"""Safety and routing checks for Russian Silero pronunciation."""
import contextlib
import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import tts
from tts_pronunciation import SileroPronunciation
import tts_worker


class PronunciationTests(unittest.TestCase):
    def processor(self, response):
        processor = SileroPronunciation.__new__(SileroPronunciation)
        processor.markup = "plus-before-vowel"
        processor.accentor = Mock(return_value=response)
        return processor

    def test_removed_options_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'book.txt'
            path.write_text('Русский текст.')
            for option in ('--debug', '11', '12', '13', '14'):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    tts.parse_args([str(path), option])

    def test_silero_receives_native_stress(self):
        result = self.processor('Он пошёл друг+ой дор+огой.')('Он пошел другой дорогой.')
        self.assertEqual(result, 'Он пошёл друг+ой дор+огой.')

    def test_worker_preprocesses_only_russian_silero(self):
        original = 'Он пошел другой дорогой.'
        marked = 'Он пошёл друг+ой дор+огой.'
        for engine, language in [('Silero', 'ru'), ('Silero', 'ru-RU'), ('Silero', 'en'),
                                 ('Edge', 'ru'), ('Google', 'ru'), ('Piper', 'ru'), ('RHVoice', 'ru')]:
            with self.subTest(engine=engine, language=language), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                source = directory / 'book.txt'
                source.write_text(original)
                synth = Mock(engine=engine, language=language, voice='test')
                cache = Mock(directory=directory, limit=0)
                cache.get.return_value = None
                accentor = Mock(identity={'test': 'stress'}, return_value=marked)
                with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
                     patch.object(tts_worker, 'voice_identity', return_value='voice'), \
                     patch.object(tts_worker, 'PCMCache', return_value=cache), \
                     patch.object(tts_worker, 'decode_pcm', return_value=b'\0\0'), \
                     patch.object(tts_worker, 'continuous_rhvoice', return_value=iter([b'\0\0'])) as native, \
                     patch('tts_pronunciation.SileroPronunciation', return_value=accentor) as load:
                    tts_worker.produce({'text_file': str(source), 'mode': 1, 'language': language,
                                        'bookmark': {'seconds': 0, 'voice': ''}}, directory, io.BytesIO())
                    enabled = engine == 'Silero' and language.split('-')[0] == 'ru'
                    self.assertEqual(load.call_count, int(enabled))
                    if engine == 'RHVoice':
                        self.assertEqual(native.call_args.args[1], original)
                    else:
                        self.assertEqual(synth.generate.call_args.args[0], marked if enabled else original)

    def test_rejects_truncation_letter_punctuation_and_case_changes(self):
        text = 'Он пошел другой дорогой.'
        for result in ('Он пошёл.', 'Он пришёл друг+ой дор+огой.',
                       'Он пошёл друг+ой дор+огой!', 'он пошёл друг+ой дор+огой.'):
            with self.assertLogs('tts_pronunciation', level='WARNING'):
                self.assertEqual(self.processor(result)(text), text)

    def test_preserves_literal_plus_signs(self):
        text = 'C++ и 2+2, дорога.'
        self.assertEqual(self.processor('C++ и 2+2, дор+ога.')(text), 'C++ и 2+2, дор+ога.')

    def test_inference_failure_preserves_original(self):
        processor = self.processor('')
        processor.accentor.side_effect = RuntimeError('model failure')
        with self.assertLogs('tts_pronunciation', level='ERROR') as logs:
            self.assertEqual(processor('Текст.'), 'Текст.')
        self.assertIn('model failure', '\n'.join(logs.output))



if __name__ == '__main__':
    unittest.main()
