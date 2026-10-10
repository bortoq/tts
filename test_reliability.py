"""Regression tests for audit fixes; all network responses are controlled."""
import importlib.util
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import zipfile

import tts_books
import tts_engines
import tts_network
import tts_pipeline
import tts_state
import tts_text
import tts_voices
import tts_worker


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = patch.dict(os.environ, {'TTS_CACHE_DIR': str(self.directory / 'cache'),
                                             'TTS_VOICE_CONFIG': str(self.directory / 'voices.json')})
        environment.start()
        self.addCleanup(environment.stop)

    def test_scripts_and_regions_matrix(self):
        for requested, offered, compatible in (
            ('zh-Hant', 'zh-Hans-CN', False), ('zh-Hans', 'zh-Hant-TW', False),
            ('zh-Hant-HK', 'zh-CN', False), ('zh-Hans-CN', 'zh-TW', False),
            ('sr-Latn', 'sr-Cyrl', False), ('sr-Cyrl-RS', 'sr-Latn-RS', False),
            ('zh-Hant', 'zh-TW', True), ('zh-Hans', 'zh-SG', True),
            ('sr-Latn', 'sr-Latn-RS', True), ('pt-PT', 'pt-BR', True),
        ):
            with self.subTest(requested=requested, offered=offered):
                self.assertEqual(tts_voices.locale_score(requested, offered) >= 0, compatible)
        voices = [{'name': 'BR', 'language': 'pt-BR', 'gender': 'female'},
                  {'name': 'PT', 'language': 'pt-PT', 'gender': 'female'}]
        self.assertEqual(tts_voices.select_voice(voices, 'pt-PT', 'female')['name'], 'PT')

    def test_piper_cached_exact_locale_beats_installed_other_region(self):
        index = self.directory / 'index.json'
        index.write_text(json.dumps({'en_GB-alba-medium': {
            'language': {'code': 'en_GB'}, 'name': 'alba', 'quality': 'medium',
            'files': {'en/en_GB/alba/medium/en_GB-alba-medium.onnx': {},
                      'en/en_GB/alba/medium/en_GB-alba-medium.onnx.json': {}}}}))
        local = [{'name': 'en_US-amy-medium', 'language': 'en-US', 'gender': 'female',
                  'path': '/installed/amy.onnx'}]
        with patch.dict(os.environ, {'TTS_PIPER_INDEX': str(index)}), \
             patch.object(tts_voices, 'local_piper_catalog', return_value=local), \
             patch.object(tts_voices, 'download') as download:
            selected = tts_voices.piper_voice('en-GB', 'female', 'amy')
            self.assertEqual(selected['name'], 'en_GB-alba-medium')
            self.assertEqual(download.call_count, 2)

    def test_shared_sentence_delimiters_and_abbreviations(self):
        fixtures = (
            ('「こんにちは。」次です。', ['「こんにちは。」', '次です。']),
            ('『你好！』下一句。', ['『你好！』', '下一句。']),
            ('Dr. Smith met A. B. Jones. The value is 3.14.',
             ['Dr. Smith met A. B. Jones.', 'The value is 3.14.']),
            ('«Да!» Она ушла. Потом… Тишина.', ['«Да!»', 'Она ушла.', 'Потом…', 'Тишина.']),
            ('Mme. Dupont arrive. Bonjour !', ['Mme. Dupont arrive.', 'Bonjour !']),
        )
        for text, expected in fixtures:
            self.assertEqual(list(tts_text.sentences(text)), expected)
            for limit in (20, 100, 800):
                parts = list(tts_text.text_parts(text, limit))
                self.assertTrue(all(0 < len(part) <= limit for part in parts))
                self.assertEqual(''.join(''.join(parts).split()), ''.join(text.split()))

    def test_format_and_zip_size_limits(self):
        pdf = self.directory / 'book.pdf'
        pdf.write_bytes(b'%PDF-1.4 binary')
        with self.assertRaisesRegex(ValueError, 'Supported formats'):
            tts_books.read_document(pdf)
        book = self.directory / 'book.fb2.zip'
        with zipfile.ZipFile(book, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('book.fb2', '<FictionBook><body><p>' + 'x' * 5000 + '</p></body></FictionBook>')
        with patch.dict(os.environ, {'TTS_MAX_BOOK_BYTES': '1024'}):
            with self.assertRaisesRegex(ValueError, 'Uncompressed'):
                tts_books.read_document(book)
        text, language = tts_books.read_document(book)
        self.assertEqual((len(text), language), (5000, 'ru'))

    def test_fb2_streaming_keeps_nested_inline_text_and_ignores_metadata(self):
        source = self.directory / 'book.fb2'
        source.write_text('<FictionBook><description><title-info><lang>en</lang><p>IGNORE</p>'
                          '</title-info></description><body><section>' +
                          '<p>Before <emphasis>middle <strong>deep</strong></emphasis> after.</p>' * 5000 +
                          '</section></body><binary>IGNORE</binary></FictionBook>')
        text, language = tts_books.read_document(source)
        self.assertEqual(language, 'en')
        self.assertEqual(text.splitlines(), ['Before middle deep after.'] * 5000)

    def test_retry_transient_statuses_and_retry_after(self):
        for code in (429, 500, 502, 503, 504):
            failure = urllib.error.HTTPError('url', code, 'temporary', {'Retry-After': '1.25'}, None)
            operation = MagicMock(side_effect=[failure, 'ok'])
            with patch.object(tts_network.time, 'sleep') as sleep:
                self.assertEqual(tts_network.retry(operation), 'ok')
                sleep.assert_called_once_with(1.25)
        failure = urllib.error.HTTPError('url', 403, 'forbidden', {}, None)
        operation = MagicMock(side_effect=failure)
        with self.assertRaises(urllib.error.HTTPError):
            tts_network.retry(operation)
        operation.assert_called_once()
        operation = MagicMock(side_effect=ConnectionError('offline'))
        with patch.object(tts_network.time, 'sleep'), self.assertRaises(ConnectionError):
            tts_network.retry(operation)
        self.assertEqual(operation.call_count, 4)

    def test_download_checks_cached_hash_and_recovers(self):
        destination = self.directory / 'model.pt'
        destination.write_bytes(b'corrupt')
        payload = b'valid model'
        with patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            tts_voices.download('url', destination, len(payload), hashlib.md5(payload).hexdigest())
        self.assertEqual(destination.read_bytes(), payload)
        with patch.object(tts_network.urllib.request, 'urlopen') as request:
            tts_voices.download('url', destination, len(payload), hashlib.md5(payload).hexdigest())
            request.assert_not_called()

    def test_incomplete_http_body_is_retried_and_not_cached(self):
        response = io.BytesIO(b'short')
        response.headers = {'Content-Length': '50'}
        complete = io.BytesIO(b'complete')
        complete.headers = {'Content-Length': '8'}
        with patch.object(tts_network.urllib.request, 'urlopen', side_effect=[response, complete]), \
             patch.object(tts_network.time, 'sleep'):
            path = tts_voices.download('url', self.directory / 'model.pt')
        self.assertEqual(path.read_bytes(), b'complete')
        self.assertFalse(list(self.directory.glob('*.part')))

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_corrupt_package_is_removed_then_redownloaded(self):
        import torch
        model_path = tts_engines.silero_path({'model': 'test', 'url': 'url', 'trusted_override': True})
        model_path.parent.mkdir(parents=True)
        model_path.write_bytes(b'corrupt package')
        good_importer = MagicMock()
        with patch.object(torch.package, 'PackageImporter', side_effect=[ValueError('corrupt'), good_importer]), \
             patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(b'good package')) as request:
            tts_engines.load_silero({'model': 'test', 'url': 'url', 'trusted_override': True})
        request.assert_called_once()
        self.assertEqual(model_path.read_bytes(), b'good package')

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_empty_download_does_not_poison_cache(self):
        import torch
        with patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(b'')), \
             patch.object(torch.package, 'PackageImporter') as importer:
            with self.assertRaisesRegex(RuntimeError, 'Empty download'):
                tts_engines.load_silero({'model': 'test', 'url': 'url', 'trusted_override': True})
            importer.assert_not_called()
        self.assertFalse(tts_engines.silero_path({'model': 'test', 'url': 'url', 'trusted_override': True}).exists())

    def test_pcm_cache_validates_data_and_evicts(self):
        with patch.dict(os.environ, {'TTS_PCM_CACHE_MB': '1'}):
            cache = tts_state.PCMCache()
            cache.put('first', b'\x01\x00' * 300000)
            self.assertEqual(len(cache.get('first')), 600000)
            cache.put('second', b'\x02\x00' * 300000)
            self.assertIsNone(cache.get('first'))
            self.assertIsNotNone(cache.get('second'))
            (cache.directory / 'second.pcm').write_bytes(b'broken')
            self.assertIsNone(cache.get('second'))
            self.assertFalse((cache.directory / 'second.json').exists())

    def test_checkpoint_uses_played_time_plus_resume_offset(self):
        bookmark = tts_state.Bookmark('text', 9, 'en')
        (self.directory / 'voice.json').write_text(json.dumps({'offset': 12, 'voice': 'fingerprint'}))
        (self.directory / 'mpv-position.json').write_text(json.dumps({'seconds': 3.25, 'speed': 2.3}))
        tts_pipeline.PositionMonitor(self.directory, bookmark).checkpoint()
        self.assertEqual(bookmark.read(), {'seconds': 15.25, 'voice': 'fingerprint', 'speed': 2.3})
        self.assertEqual(tts_state.Bookmark('different text', 9, 'en').read()['seconds'], 0)
        bookmark.clear()
        self.assertEqual(bookmark.read()['seconds'], 0)

    def test_google_worker_cache_and_resume_skip_actual_pcm_frames(self):
        source = self.directory / 'book.txt'
        source.write_text('First sentence. Second sentence.')
        synth = MagicMock(engine='Google', voice='en', language='en')
        synth.generate.return_value = self.directory / 'audio.mp3'
        job = {'text_file': str(source), 'mode': 9, 'language': 'en',
               'bookmark': {'seconds': 0, 'voice': ''}}
        class Output:
            def __init__(self):
                self.buffer = io.BytesIO()
        output = Output()
        pcm = b'\x01\x00' * 48000
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'decode_pcm', return_value=pcm) as decode, \
             patch.object(tts_worker.sys, 'stdout', output), contextlib.redirect_stderr(io.StringIO()):
            tts_worker.produce(job, self.directory)
            self.assertEqual(output.buffer.getvalue(), pcm)
            output.buffer = io.BytesIO()
            job['bookmark'] = {'seconds': 0.75, 'voice': 'voice'}
            tts_worker.produce(job, self.directory)
            self.assertEqual(output.buffer.getvalue(), pcm[36000:])
            decode.assert_called_once()
            synth.generate.assert_called_once_with('First sentence. Second sentence.', self.directory)

    def test_worker_cancellation_kills_active_descendants_and_cleans_partial(self):
        for stage in ('download', 'inference', 'decode'):
            with self.subTest(stage=stage):
                ready = self.directory / (stage + '.pid')
                code = '''import os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
cache = pathlib.Path(os.environ['TTS_CACHE_DIR']); cache.mkdir(exist_ok=True)
(cache / ('tts-' + str(os.getpid()) + '-download.part')).write_bytes(b'incomplete')
time.sleep(60)
'''
                command = [sys.executable, '-c', code, str(ready)]
                start = time.monotonic()
                with tts_pipeline.AudioWorker(self.directory, 2, command) as worker:
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    child = int(ready.read_text())
                self.assertLess(time.monotonic() - start, 4)
                self.assertFalse(worker.thread.is_alive())
                self.assertIsNotNone(worker.process.poll())
                status = Path(f'/proc/{child}/stat')
                if status.exists():
                    self.assertEqual(status.read_text().split()[2], 'Z')
                self.assertFalse(list(tts_voices.cache_dir().glob('*.part')))

    def test_buffer_duration_scales_with_speed_and_caps_queue(self):
        for speed in (1, 2.3, 4):
            target = tts_pipeline.buffer_seconds(speed)
            self.assertAlmostEqual(target, 8 * speed)
            code = ('import sys,time\n' +
                    'for _ in range(200):\n sys.stdout.buffer.write(b"\\1\\0"*24000); sys.stdout.buffer.flush(); time.sleep(0.002)\n')
            with tts_pipeline.AudioWorker(self.directory, target, [sys.executable, '-c', code]) as worker:
                worker.prefill(MagicMock())
                self.assertGreaterEqual(worker.buffered, target)
                time.sleep(0.1)
                self.assertLessEqual(worker.buffered, 2 * target + 1)
                self.assertEqual(len(worker.get(MagicMock())), 48000)

    def test_short_audio_with_variable_delays_and_speed_updates(self):
        with patch.dict(os.environ, {'TTS_BUFFER_SECONDS': '0.25'}):
            for speed in (1, 2.3, 4):
                target = tts_pipeline.buffer_seconds(speed)
                code = ('import sys,time\nfor i in range(100):\n '
                        'time.sleep(0.025 if i % 3 == 0 else 0.001); '
                        'sys.stdout.buffer.write(bytes([1,0])*2400);sys.stdout.buffer.flush()\n')
                with tts_pipeline.AudioWorker(self.directory, target, [sys.executable, '-c', code], speed=speed) as worker:
                    worker.prefill(MagicMock())
                    self.assertGreaterEqual(worker.buffered, target)
                    self.assertEqual(len(worker.get(MagicMock())), 48000)
                    (self.directory / 'mpv-position.json').write_text(json.dumps({'speed': 2.3}))
                    worker.update_speed()
                    self.assertAlmostEqual(worker.target, 0.25 * 2.3)
                    (self.directory / 'mpv-position.json').unlink()

    @unittest.skipUnless(importlib.util.find_spec('torch'), 'torch optional dependency is required')
    def test_silero_download_validates_before_atomic_commit(self):
        import torch
        spec = {'model': 'bad', 'url': 'url', 'trusted_override': True}
        with patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(b'not a package')), \
             patch.object(torch.package, 'PackageImporter', side_effect=ValueError('invalid package')):
            with self.assertRaisesRegex(ValueError, 'invalid package'):
                tts_engines.load_silero(spec)
        self.assertFalse(tts_engines.silero_path(spec).exists())
        self.assertFalse(list(tts_voices.cache_dir().glob('*.part')))

    def test_google_retries_incomplete_response_body(self):
        import http.client
        import tts_google
        audio = b'valid audio'
        import base64
        response = json.dumps([['wrb.fr', 'jQ1olc', json.dumps([base64.b64encode(audio).decode()])]]).encode()
        broken = MagicMock()
        broken.__enter__.return_value = broken
        broken.read.side_effect = http.client.IncompleteRead(b'partial')
        with patch.object(tts_google.urllib.request, 'urlopen', side_effect=[broken, io.BytesIO(response)]) as request, \
             patch.object(tts_network.time, 'sleep'):
            tts_google.save('Hello.', 'en', self.directory / 'audio.mp3', 1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual((self.directory / 'audio.mp3').read_bytes(), audio)


if __name__ == '__main__':
    unittest.main()
