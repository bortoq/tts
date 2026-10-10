"""Regression tests for the external audit's resource and trust boundaries."""
import hashlib
import contextlib
import importlib.util
import io
import json
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

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'yaml optional dependency is required')
    def test_unverified_catalog_cannot_supply_its_own_trusted_hash(self):
        data = json.dumps({'tts_models': {'new': {'v1_new': {'latest': {
            'package': 'https://example.invalid/untrusted.pt', 'sha256_digest': 'a' * 64}}}}}).encode()
        path = self.root / 'untrusted.yml'
        path.write_bytes(data)
        for configuration in ({'TTS_SILERO_INDEX': str(path)}, {'TTS_SILERO_REVISION': 'untrusted'}):
            with self.subTest(configuration=configuration), patch.dict(os.environ, configuration), \
                 patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
                spec = tts_voices.extra_silero_model('new')
                self.assertNotIn('sha256_digest', spec)
                self.assertNotIn('trusted_override', spec)
                with self.assertRaisesRegex(ValueError, 'sha256_digest'):
                    tts_engines.load_silero(spec)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'yaml optional dependency is required')
    def test_unverified_catalog_cannot_replace_builtin_pin(self):
        pin = tts_voices.SILERO_PINS['v4_ru']
        path = self.root / 'catalog.yml'
        path.write_text(json.dumps({'tts_models': {'new': {'v1_new': {'latest': {
            'package': pin['url'], 'sha256_digest': 'a' * 64}}}}}))
        with patch.dict(os.environ, {'TTS_SILERO_INDEX': str(path)}):
            spec = tts_voices.extra_silero_model('new')
        self.assertEqual(spec['sha256_digest'], pin['sha256_digest'])
        self.assertEqual(spec['size_bytes'], pin['size_bytes'])

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'yaml optional dependency is required')
    def test_verified_default_catalog_can_supply_model_pin(self):
        data = json.dumps({'tts_models': {'new': {'v1_new': {'latest': {
            'package': 'https://example.invalid/reviewed.pt', 'sha256_digest': 'a' * 64}}}}}).encode()
        with patch.dict(tts_voices.SILERO_CATALOG, {'sha256_digest': hashlib.sha256(data).hexdigest()}), \
             patch.object(tts_network.urllib.request, 'urlopen', return_value=io.BytesIO(data)):
            spec = tts_voices.extra_silero_model('new')
        self.assertEqual(spec['sha256_digest'], 'a' * 64)

    def test_cached_pcm_limit_is_enforced_before_reading_bytes(self):
        cache = tts_state.PCMCache()
        cache.put('large', b'\1\0' * 32)
        pcm_path = cache.directory / 'large.pcm'
        original = Path.open
        def checked_open(path, *args, **kwargs):
            if path == pcm_path:
                raise AssertionError('Oversized PCM must be rejected before opening it')
            return original(path, *args, **kwargs)
        with patch.dict(os.environ, {'TTS_MAX_PCM_BYTES': '16'}), \
             patch.object(Path, 'open', checked_open):
            self.assertIsNone(cache.get('large'))
        self.assertFalse(pcm_path.exists())

    def test_cached_pcm_actual_size_must_match_metadata_before_read(self):
        cache = tts_state.PCMCache()
        cache.put('large', b'\1\0' * 32)
        metadata = cache.directory / 'large.json'
        data = json.loads(metadata.read_text())
        data['bytes'] = 2
        metadata.write_text(json.dumps(data))
        with patch.dict(os.environ, {'TTS_MAX_PCM_BYTES': '16'}):
            self.assertIsNone(cache.get('large'))

    def test_cache_does_not_write_entries_over_current_pcm_limit(self):
        with patch.dict(os.environ, {'TTS_MAX_PCM_BYTES': '16'}):
            cache = tts_state.PCMCache()
            cache.put('oversized', b'\1\0' * 32)
            self.assertFalse((cache.directory / 'oversized.pcm').exists())

    def test_native_resume_regenerates_cached_blocks_over_lowered_limit(self):
        source = self.root / 'book.txt'
        source.write_text('Непрерывное чтение.')
        job = {'text_file': str(source), 'mode': 1, 'language': 'ru',
               'bookmark': {'seconds': 0, 'voice': ''}}
        blocks = [b'\1\0' * 24000, b'\2\0' * 24000]
        synth = Mock(engine='RHVoice', language='ru', voice='Anna')
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'continuous_rhvoice', return_value=iter(blocks)):
            tts_worker.produce(job, self.root, io.BytesIO())
        job['bookmark'] = {'seconds': 1.5, 'voice': 'voice'}
        output = io.BytesIO()
        with patch.dict(os.environ, {'TTS_MAX_PCM_BYTES': '16'}), \
             patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'continuous_rhvoice', return_value=iter(blocks)) as regenerate:
            tts_worker.produce(job, self.root, output)
        regenerate.assert_called_once()
        self.assertEqual(output.getvalue(), blocks[1][24000:])

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
