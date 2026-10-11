"""Shared cache accounting, legacy interoperability and recovery regressions."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from tts_state import PCMCache, atomic_write


class CacheAccountingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {'TTS_CACHE_DIR': temporary.name})
        env.start()
        self.addCleanup(env.stop)

    def test_hits_and_shared_writes_do_not_rescan_directory(self):
        first, second = PCMCache(), PCMCache()
        with patch.object(PCMCache, '_reconcile', side_effect=AssertionError('Unexpected scan')):
            for i in range(100):
                first.put(str(i), b'\1\0' * 8)
                self.assertEqual(second.get(str(i)), b'\1\0' * 8)
            self.assertIsNone(first.get('missing'))
            self.assertEqual(second.used, 1600)

    def test_interrupted_metadata_write_is_reconciled(self):
        cache = PCMCache()
        original = atomic_write
        def fail_metadata(path, data):
            if Path(path).suffix == '.json':
                raise OSError('Interrupted write')
            original(path, data)
        with patch('tts_state.atomic_write', side_effect=fail_metadata):
            with self.assertRaises(OSError):
                cache.put('partial', b'\1\0' * 8)
        self.assertIsNone(cache.get('partial'))
        self.assertEqual(cache.used, 0)
        cache.put('complete', b'\1\0' * 8)
        self.assertEqual(cache.used, 16)

    def test_legacy_writer_is_detected_and_lower_budget_is_enforced(self):
        with patch.dict(os.environ, {'TTS_PCM_CACHE_MB': '1'}):
            reader = PCMCache()
        reader.put('old', b'\1\0' * 300000)
        # Previous releases update the old access table only, with default
        # DELETE journaling and atomic PCM/JSON replacements under the lock.
        data = b'\2\0' * 300000
        with reader.locked():
            atomic_write(reader.directory / 'legacy.pcm', data)
            atomic_write(reader.directory / 'legacy.json', json.dumps({
                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}).encode())
            connection = sqlite3.connect(reader.directory / 'lru.sqlite3')
            try:
                with connection:
                    connection.execute('INSERT INTO access VALUES (?, 1000)', ('legacy',))
            finally:
                connection.close()
        self.assertEqual(reader.get('legacy'), data)
        self.assertIsNone(reader.get('old'))
        self.assertEqual(reader.used, len(data))
        reader.put('new', b'\3\0' * 300000)
        self.assertIsNone(reader.get('legacy'))
        self.assertIsNotNone(reader.get('new'))

    def test_legacy_reads_preserve_lru_without_directory_changes(self):
        with patch.dict(os.environ, {'TTS_PCM_CACHE_MB': '1'}):
            cache = PCMCache()
        data = b'\1\0' * 200000
        cache.put('a', data)
        cache.put('b', data)
        stamp = cache._stamp()
        with patch.object(PCMCache, '_reconcile', side_effect=AssertionError('Unexpected scan')):
            # This is 2ba2d7c's read/access transaction: it knows only access,
            # uses MAX+1, and replaces the previously read key's sequence.
            for _ in range(10):
                with cache.locked(), closing(sqlite3.connect(cache.directory / 'lru.sqlite3')) as index:
                    # Hold journal names stable to exercise the audited case
                    # independently of filesystem/SQLite DELETE-journal behavior.
                    index.execute('PRAGMA journal_mode=TRUNCATE')
                    self.assertEqual((cache.directory / 'b.pcm').read_bytes(), data)
                    with index:
                        index.execute('INSERT OR REPLACE INTO access VALUES (?, '
                                      '(SELECT COALESCE(MAX(sequence), 0) + 1 FROM access))', ('b',))
            self.assertEqual(cache._stamp(), stamp)
            self.assertEqual(cache.get('a'), data)
            with closing(sqlite3.connect(cache.directory / 'lru.sqlite3')) as index:
                order = dict(index.execute('SELECT key, sequence FROM access'))
            self.assertGreater(order['a'], order['b'])
            cache.put('c', data)
            self.assertIsNone(cache.get('b'))
            self.assertEqual(cache.get('a'), data)
            self.assertEqual(cache.get('c'), data)

    def test_legacy_sequence_updates_advance_counter_and_rollback_atomically(self):
        cache = PCMCache()
        cache.put('a', b'\1\0')
        with cache.locked(), closing(sqlite3.connect(cache.directory / 'lru.sqlite3')) as index:
            with index:
                index.execute('UPDATE access SET sequence=100 WHERE key=?', ('a',))
            self.assertEqual(index.execute('SELECT sequence FROM accounting').fetchone()[0], 100)
            try:
                with index:
                    index.execute('UPDATE access SET sequence=1000 WHERE key=?', ('a',))
                    raise ValueError('Rollback')
            except ValueError:
                pass
            self.assertEqual(index.execute('SELECT sequence FROM accounting').fetchone()[0], 100)
        self.assertIsNotNone(cache.get('a'))
        with closing(sqlite3.connect(cache.directory / 'lru.sqlite3')) as index:
            self.assertEqual(index.execute('SELECT sequence FROM access WHERE key=?', ('a',)).fetchone()[0], 101)

    def test_upgrade_synchronizes_legacy_reads_before_trigger_installation(self):
        cache = PCMCache()
        cache.put('a', b'\1\0')
        with cache.locked(), closing(sqlite3.connect(cache.directory / 'lru.sqlite3')) as index:
            index.execute('PRAGMA journal_mode=TRUNCATE')
            with index:
                index.execute('DROP TRIGGER access_sequence_insert')
                index.execute('DROP TRIGGER access_sequence_update')
                index.execute('UPDATE access SET sequence=100 WHERE key=?', ('a',))
            self.assertEqual(index.execute('SELECT sequence FROM accounting').fetchone()[0], 1)
        stamp = cache._stamp()
        upgraded = PCMCache()
        self.assertEqual(upgraded._stamp(), stamp)
        self.assertIsNotNone(upgraded.get('a'))
        with closing(sqlite3.connect(cache.directory / 'lru.sqlite3')) as index:
            self.assertEqual(index.execute('SELECT sequence FROM access WHERE key=?', ('a',)).fetchone()[0], 101)

    def test_sqlite_connections_close_on_success_and_failure(self):
        connections = []
        connect = sqlite3.connect
        def tracked_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection
        with patch('tts_state.sqlite3.connect', side_effect=tracked_connect):
            cache = PCMCache()
            cache.put('valid', b'\1\0')
            self.assertEqual(cache.get('valid'), b'\1\0')
            with self.assertRaises(ValueError):
                cache.put('invalid', b'odd')
        self.assertEqual(len(connections), 4)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')

    def test_replaced_and_deleted_entries_update_size(self):
        cache = PCMCache()
        cache.put('entry', b'\1\0' * 8)
        cache.put('entry', b'\2\0' * 16)
        self.assertEqual(cache.used, 32)
        (cache.directory / 'entry.pcm').unlink()
        self.assertIsNone(cache.get('entry'))
        self.assertEqual(cache.used, 0)
        self.assertEqual(PCMCache().used, 0)


if __name__ == '__main__':
    unittest.main()
