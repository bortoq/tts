"""Regression checks for text bookmarks, concurrent cache use and diagnostics."""
import io
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import tts_pipeline
import tts_state
import tts_worker


def write_cache(cache_root, entered, release):
    os.environ['TTS_CACHE_DIR'] = cache_root
    cache = tts_state.PCMCache()
    original = tts_state.atomic_write

    def slow_write(path, data):
        original(path, data)
        if Path(path).suffix == '.pcm':
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Reader did not release writer')
    with patch.object(tts_state, 'atomic_write', side_effect=slow_write):
        cache.put('shared', b'\x02\x00' * 10)


def read_cache(cache_root, started, result):
    os.environ['TTS_CACHE_DIR'] = cache_root
    started.set()
    result.put(tts_state.PCMCache().get('shared'))


class AuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'TTS_CACHE_DIR': str(self.root / 'cache')})
        env.start()
        self.addCleanup(env.stop)

    def test_cache_reader_waits_for_complete_cross_process_commit(self):
        cache = tts_state.PCMCache()
        cache.put('shared', b'\x01\x00' * 4)
        ctx = multiprocessing.get_context('fork')
        entered, release, started = ctx.Event(), ctx.Event(), ctx.Event()
        result = ctx.Queue()
        writer = ctx.Process(target=write_cache, args=(str(self.root / 'cache'), entered, release))
        reader = ctx.Process(target=read_cache, args=(str(self.root / 'cache'), started, result))
        try:
            writer.start()
            self.assertTrue(entered.wait(5))
            reader.start()
            self.assertTrue(started.wait(5))
            reader.join(0.2)
            self.assertTrue(reader.is_alive())
            release.set()
            self.assertEqual(result.get(timeout=5), b'\x02\x00' * 10)
            writer.join(5)
            reader.join(5)
            self.assertEqual(writer.exitcode, 0)
            self.assertEqual(reader.exitcode, 0)
            self.assertEqual(cache.get('shared'), b'\x02\x00' * 10)
        finally:
            release.set()
            for process in (writer, reader):
                if process.pid:
                    process.join(2)
                    if process.is_alive():
                        process.kill()
                        process.join()
            result.close()

    def test_independent_cache_instances_enforce_shared_size_limit(self):
        with patch.dict(os.environ, {'TTS_PCM_CACHE_MB': '1'}):
            first, second = tts_state.PCMCache(), tts_state.PCMCache()
            first.put('first', b'\1\0' * 300000)
            second.put('second', b'\2\0' * 300000)
            self.assertIsNone(first.get('first'))
            self.assertEqual(second.get('second'), b'\2\0' * 300000)
            self.assertLessEqual(sum(p.stat().st_size for p in first.directory.glob('*.pcm')), first.limit)

    def test_resume_skips_prior_text_with_evicted_audio_and_changed_duration(self):
        source = self.root / 'book.txt'
        first, second = 'Первое ' + 'слово ' * 11 + '.', 'Второе ' + 'слово ' * 11 + '.'
        source.write_text(first + ' ' + second)
        synth = Mock(engine='Google', language='ru', voice='ru')
        job = {'text_file': str(source), 'mode': 9, 'language': 'ru',
               'bookmark': {'seconds': 0, 'voice': ''}}
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'decode_pcm', side_effect=[b'\1\0'*24000, b'\2\0'*48000]):
            tts_worker.produce(job, self.root, io.BytesIO())
        (self.root / 'mpv-position.json').write_text(json.dumps({'seconds': 1.5, 'speed': 2.3}))
        bookmark = tts_state.Bookmark(source.read_text(), 9, 'ru')
        tts_pipeline.PositionMonitor(self.root, bookmark).checkpoint()
        saved = bookmark.read()
        self.assertEqual(saved['cursor']['index'], 1)
        self.assertAlmostEqual(saved['cursor']['fraction'], 0.25)
        for path in (self.root / 'cache/pcm').glob('*.pcm'):
            path.unlink()
        synth.reset_mock()
        job['bookmark'] = saved
        output = io.BytesIO()
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'decode_pcm', return_value=b'\3\0'*96000):
            tts_worker.produce(job, self.root, output)
        synth.generate.assert_called_once_with(second, self.root)
        # Changed timing replays only the current fragment, never prior text.
        self.assertEqual(output.getvalue(), b'\3\0'*96000)

    def test_resume_same_audio_preserves_exact_offset_without_prior_synthesis(self):
        source = self.root / 'book.txt'
        source.write_text('Первое ' + 'слово ' * 11 + '. Второе ' + 'слово ' * 11 + '.')
        synth = Mock(engine='Google', language='ru', voice='ru')
        job = {'text_file': str(source), 'mode': 9, 'language': 'ru',
               'bookmark': {'seconds': 0, 'voice': ''}}
        pcm = b'\2\0' * 48000
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'decode_pcm', return_value=pcm):
            tts_worker.produce(job, self.root, io.BytesIO())
        (self.root / 'mpv-position.json').write_text(json.dumps({'seconds': 2.5}))
        bookmark = tts_state.Bookmark(source.read_text(), 9, 'ru')
        tts_pipeline.PositionMonitor(self.root, bookmark).checkpoint()
        job['bookmark'] = bookmark.read()
        output = io.BytesIO()
        synth.reset_mock()
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'):
            tts_worker.produce(job, self.root, output)
        synth.generate.assert_not_called()
        self.assertEqual(output.getvalue(), pcm[24000:])

    def test_partial_timeline_record_does_not_discard_last_valid_cursor(self):
        (self.root / 'voice.json').write_text(json.dumps({'offset': 0, 'voice': 'voice'}))
        (self.root / 'mpv-position.json').write_text(json.dumps({'seconds': 1}))
        record = {'index': 2, 'layout': 'layout', 'start': 0, 'cut': 0, 'duration': 2}
        (self.root / 'segments.jsonl').write_text(json.dumps(record) + '\n{"index":')
        bookmark = tts_state.Bookmark('text', 3, 'ru')
        tts_pipeline.PositionMonitor(self.root, bookmark).checkpoint()
        self.assertEqual(bookmark.read()['cursor'], {'index': 2, 'fraction': 0.5, 'layout': 'layout'})

    def test_q_reset_does_not_keep_text_cursor(self):
        bookmark = tts_state.Bookmark('text', 3, 'ru')
        bookmark.save(5, 'voice', state={'volume': 37}, cursor={'cursor': {'index': 2, 'fraction': 0.5, 'layout': 'layout'}})
        saved = bookmark.read()
        bookmark.save(0, saved['voice'], saved.get('speed'), saved)
        self.assertNotIn('cursor', bookmark.read())
        self.assertEqual(bookmark.read()['volume'], 37)

    def test_online_cache_survives_calendar_change(self):
        import datetime
        source = self.root / 'book.txt'
        source.write_text('Привет.')
        synth = Mock(engine='Edge', language='ru', voice='ru')
        job = {'text_file': str(source), 'mode': 3, 'language': 'ru',
               'bookmark': {'seconds': 0, 'voice': ''}}
        clock = Mock()
        clock.today.return_value = datetime.date(2026, 10, 9)
        with patch('datetime.date', clock), \
             patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'decode_pcm', return_value=b'\1\0'*24000) as decode:
            tts_worker.produce(job, self.root, io.BytesIO())
            clock.today.return_value = Mock(isoformat=lambda: '2026-10-10')
            tts_worker.produce(job, self.root, io.BytesIO())
        decode.assert_called_once()

    def test_native_cached_resume_does_not_read_prefix(self):
        source = self.root / 'book.txt'
        source.write_text('Непрерывная речь.')
        synth = Mock(engine='RHVoice', language='ru', voice='Anna')
        job = {'text_file': str(source), 'mode': 1, 'language': 'ru',
               'bookmark': {'seconds': 0, 'voice': ''}}
        blocks = [bytes([value, 0])*24000 for value in (1, 2, 3)]
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'continuous_rhvoice', return_value=iter(blocks)):
            tts_worker.produce(job, self.root, io.BytesIO())
        manifest = next((self.root / 'cache/pcm').glob('*.book.json'))
        keys = json.loads(manifest.read_text())
        # Eviction of already-played audio must not force native resynthesis.
        (self.root / 'cache/pcm' / (keys[0] + '.pcm')).unlink()
        job['bookmark'] = {'seconds': 1.5, 'voice': 'voice'}
        output = io.BytesIO()
        cache = tts_state.PCMCache()
        with patch.object(tts_worker, 'Synthesizer', return_value=synth), \
             patch.object(tts_worker, 'voice_identity', return_value='voice'), \
             patch.object(tts_worker, 'PCMCache', return_value=cache), \
             patch.object(cache, 'get', wraps=cache.get) as read, \
             patch.object(tts_worker, 'continuous_rhvoice') as native:
            tts_worker.produce(job, self.root, output)
        native.assert_not_called()
        self.assertEqual([call.args[0] for call in read.call_args_list], keys[1:])
        self.assertEqual(output.getvalue(), blocks[1][24000:] + blocks[2])

    def test_worker_diagnostics_survive_temporary_session(self):
        (self.root / 'job.json').write_text('{}')
        code = '''import logging, sys
from unittest.mock import patch
import tts_worker
with patch.object(tts_worker, 'produce', side_effect=lambda *args: logging.warning('test fallback diagnostic')):
    sys.argv = ['worker', sys.argv[1]]
    sys.exit(tts_worker.main())
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')
        logs = list((self.root / 'cache/logs').glob('*.log'))
        self.assertEqual(len(logs), 1)
        self.assertIn('test fallback diagnostic', logs[0].read_text())


if __name__ == '__main__':
    unittest.main()
