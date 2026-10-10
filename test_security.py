"""Regression tests for the external audit's resource and trust boundaries."""
import hashlib
import contextlib
import importlib.util
import io
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave
import zipfile

import tts_books
import tts_engines
import tts_network
import tts_pipeline
import tts_state
import tts_voices
import tts_worker


def concurrent_put(root, key, barrier):
    os.environ['TTS_CACHE_DIR'] = root
    os.environ['TTS_PCM_CACHE_MB'] = '1'
    cache = tts_state.PCMCache()
    barrier.wait(timeout=5)
    cache.put(key, b'\1\0' * 300000)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'TTS_CACHE_DIR': str(self.root / 'cache'),
                                    'TTS_VOICE_CONFIG': str(self.root / 'no-config')})
        env.start()
        self.addCleanup(env.stop)

    def test_eviction_and_access_order_with_identical_filesystem_timestamps(self):
        write = tts_state.atomic_write
        def frozen_write(path, data):
            write(path, data)
            if Path(path).suffix == '.pcm':
                os.utime(path, ns=(1000000000, 1000000000))
        with patch.dict(os.environ, {'TTS_PCM_CACHE_MB': '1'}), \
             patch.object(tts_state, 'atomic_write', side_effect=frozen_write):
            first, second = tts_state.PCMCache(), tts_state.PCMCache()
            for key in ('a', 'b'):
                first.put(key, b'\1\0' * 200000)
            self.assertIsNotNone(second.get('a'))
            second.put('c', b'\2\0' * 200000)
            self.assertIsNone(first.get('b'))
            self.assertIsNotNone(first.get('a'))
            self.assertIsNotNone(first.get('c'))

    def test_eviction_keeps_new_entry_across_concurrent_processes(self):
        ctx = multiprocessing.get_context('fork')
        barrier = ctx.Barrier(2)
        processes = [ctx.Process(target=concurrent_put, args=(str(self.root / 'cache'), key, barrier))
                     for key in ('first', 'second')]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            files = list((self.root / 'cache/pcm').glob('*.pcm'))
            self.assertEqual(len(files), 1)
            import sqlite3
            with sqlite3.connect(self.root / 'cache/pcm/lru.sqlite3') as index:
                newest = index.execute('SELECT key FROM access ORDER BY sequence DESC LIMIT 1').fetchone()[0]
            self.assertEqual(files[0].stem, newest)
        finally:
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join()

    def test_dtd_and_entities_rejected_in_plain_and_zipped_fb2(self):
        xml = b'<!DOCTYPE FictionBook [<!ENTITY a "hello">]><FictionBook><body><p>&a;</p></body></FictionBook>'
        for name in ('book.fb2', 'book.fb2.zip'):
            path = self.root / name
            if name.endswith('.zip'):
                with zipfile.ZipFile(path, 'w') as archive:
                    archive.writestr('book.fb2', xml)
            else:
                path.write_bytes(xml)
            with self.assertRaisesRegex(ValueError, 'DTD'):
                tts_books.read_document(path)
        # Even a DTD without entities is rejected.
        path = self.root / 'book.fb2'
        path.write_text('<!DOCTYPE FictionBook><FictionBook/>')
        with self.assertRaisesRegex(ValueError, 'DTD'):
            tts_books.read_document(path)

    def test_extracted_text_is_bounded_even_when_nested_blocks_duplicate_text(self):
        xml = '<FictionBook><body>' + '<p>' * 20 + 'я' * 100 + '</p>' * 20 + '</body></FictionBook>'
        with patch.dict(os.environ, {'TTS_MAX_BOOK_BYTES': '1024'}):
            with self.assertRaisesRegex(ValueError, 'Extracted text'):
                tts_books.parse_fb2(io.BytesIO(xml.encode()))

    def test_xml_depth_limit(self):
        xml = '<FictionBook>' + '<section>' * 130 + '</section>' * 130 + '</FictionBook>'
        with self.assertRaisesRegex(ValueError, 'depth'):
            tts_books.parse_fb2(io.BytesIO(xml.encode()))

    def test_language_aliases_without_system_registry(self):
        tts_voices.language_registry.cache_clear()
        try:
            with patch.object(Path, 'is_file', return_value=False):
                for source, expected in {'fra': 'fr', 'fre': 'fr', 'ger': 'de', 'deu': 'de',
                                         'zho-Hant': 'zh-Hant', 'chi': 'zh', 'ces': 'cs',
                                         'cze': 'cs', 'myv': 'myv'}.items():
                    self.assertEqual(tts_voices.normalize_language(source), expected)
                self.assertEqual(tts_voices.language_from_name('French'), 'fr')
        finally:
            tts_voices.language_registry.cache_clear()

    def test_rpc_rejects_declared_or_actual_oversize_without_retry(self):
        for declared in (True, False):
            response = io.BytesIO(b'x' * 17)
            if declared:
                response.headers = {'Content-Length': '17'}
            with patch.object(tts_network.urllib.request, 'urlopen', return_value=response) as request:
                with self.assertRaisesRegex(ValueError, 'byte limit'):
                    tts_network.read_url('url', max_bytes=16)
                request.assert_called_once()

    def test_body_deadline_includes_all_chunks(self):
        response = Mock(headers={})
        response.read.side_effect = [b'a', b'b', b'c', b'']
        # Time advances during successive reads even though each succeeds.
        with patch.object(tts_network.time, 'monotonic', side_effect=[0, .1, .2, .3, 1.1]):
            with self.assertRaisesRegex(TimeoutError, 'total deadline'):
                list(tts_network.response_blocks(response, 100, 1))
        self.assertEqual(response.read.call_count, 2)

    def test_stalled_dns_or_headers_does_not_block_caller_and_late_response_is_closed(self):
        release, closed = threading.Event(), threading.Event()
        response = Mock()
        response.close.side_effect = closed.set
        def delayed_open(*args, **kwargs):
            release.wait(2)
            return response
        started = time.monotonic()
        with patch.object(tts_network.urllib.request, 'urlopen', side_effect=delayed_open):
            try:
                with self.assertRaisesRegex(TimeoutError, 'opening deadline'):
                    tts_network.open_url('url', .02)
                self.assertLess(time.monotonic() - started, .5)
            finally:
                release.set()
                self.assertTrue(closed.wait(1))

    def test_verification_pins_preserve_existing_voice_identity(self):
        spec = {'model': 'fixture', 'url': 'url', 'speaker': 'xenia', 'sample_rate': 24000}
        path = tts_engines.silero_path(spec)
        path.parent.mkdir()
        path.write_bytes(b'unchanged model')
        synth = SimpleNamespace(engine='Silero', language='ru', voice='xenia', spec=spec)
        before = tts_worker.voice_identity(synth)
        spec.update(size_bytes=15, sha256_digest=hashlib.sha256(b'unchanged model').hexdigest())
        self.assertEqual(before, tts_worker.voice_identity(synth))

    def test_oversized_download_does_not_leave_partial_or_invoke_validator(self):
        validator = Mock()
        with patch.dict(os.environ, {'TTS_MAX_DOWNLOAD_BYTES': '16'}), \
             patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(b'x' * 17)):
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                tts_voices.download('url', self.root / 'model.pt', validator=validator)
        validator.assert_not_called()
        self.assertFalse(list(self.root.glob('*.part')))
        self.assertFalse((self.root / 'model.pt').exists())

    def test_model_checksum_is_checked_before_code_execution(self):
        expected = hashlib.sha256(b'approved').hexdigest()
        spec = {'model': 'fixture', 'url': 'url', 'sha256_digest': expected}
        path = tts_engines.silero_path(spec)
        path.parent.mkdir()
        path.write_bytes(b'wrong cached executable package')
        importer = Mock()
        torch = SimpleNamespace(set_num_threads=Mock(), device=Mock(),
                                package=SimpleNamespace(PackageImporter=importer))
        with patch.dict(sys.modules, {'torch': torch}), \
             patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(b'wrong downloaded package')):
            with self.assertRaisesRegex(RuntimeError, 'Checksum mismatch'):
                tts_engines.load_silero(spec)
        importer.assert_not_called()
        self.assertFalse(path.exists())

    def test_automatic_unpinned_model_is_rejected_before_loading(self):
        with self.assertRaisesRegex(ValueError, 'sha256_digest'):
            tts_engines.load_silero({'model': 'unknown', 'url': 'https://example.invalid/model'})

    def test_explicit_unpinned_override_warns_before_package_loading(self):
        calls = []
        importer = Mock(side_effect=lambda path: calls.append('load') or Mock())
        torch = SimpleNamespace(set_num_threads=Mock(), device=Mock(),
                                package=SimpleNamespace(PackageImporter=importer))
        with patch.dict(sys.modules, {'torch': torch}), \
             patch.object(tts_engines, 'print', side_effect=lambda *args, **kwargs: calls.append('warn')), \
             patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(b'valid trusted package')):
            tts_engines.load_silero({'model': 'trusted', 'url': 'url', 'trusted_override': True})
        self.assertEqual(calls, ['warn', 'load'])

    def test_unpinned_override_warning_is_visible_before_worker_start(self):
        terminal = io.StringIO()
        with patch.object(tts_pipeline, 'configured_voice', return_value={'url': 'url'}), \
             patch.object(tts_pipeline, 'Bookmark', side_effect=RuntimeError('before worker')), \
             contextlib.redirect_stderr(terminal):
            with self.assertRaisesRegex(RuntimeError, 'before worker'):
                tts_pipeline.read_aloud(self.root / 'book.txt', 5, 'ru', 'Текст', self.root, 1)
        self.assertIn('can execute code', terminal.getvalue())

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is required')
    def test_decoder_stops_oversized_pcm_and_preserves_normal_audio(self):
        path = self.root / 'speech.wav'
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(b'\1\0' * 24000)
        with patch.dict(os.environ, {'TTS_MAX_PCM_BYTES': '1024'}):
            with self.assertRaisesRegex(ValueError, 'TTS_MAX_PCM_BYTES'):
                tts_worker.decode_pcm(path)
        with patch.dict(os.environ, {'TTS_MAX_PCM_BYTES': '48000'}):
            self.assertEqual(tts_worker.decode_pcm(path), b'\1\0' * 24000)

    @unittest.skipUnless(importlib.util.find_spec('edge_tts'), 'edge optional dependency required')
    def test_edge_stream_stops_at_byte_limit(self):
        class Transport:
            def __init__(self, *args):
                pass
            async def stream(self):
                yield {'type': 'audio', 'data': b'x' * 17}
                raise AssertionError('must stop before reading next chunk')
        with patch.dict(os.environ, {'TTS_MAX_AUDIO_BYTES': '16'}), \
             patch('edge_tts.Communicate', Transport):
            with self.assertRaisesRegex(ValueError, 'TTS_MAX_AUDIO_BYTES'):
                tts_engines.Synthesizer(3, 'ru').generate('Привет.', self.root)


if __name__ == '__main__':
    unittest.main()
