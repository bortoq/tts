"""Reproducible synthetic cache-hit benchmark; accepts a baseline tts_state.py."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entries', type=int, default=4000)
    parser.add_argument('--reads', type=int, default=1000)
    parser.add_argument('--state-module', type=Path, default=Path(__file__).with_name('tts_state.py'))
    args = parser.parse_args()
    if args.entries <= 0 or args.reads <= 0:
        parser.error('entries and reads must be positive')
    spec = importlib.util.spec_from_file_location('benchmark_state', args.state_module)
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    with tempfile.TemporaryDirectory(prefix='tts-cache-benchmark-') as root:
        os.environ['TTS_CACHE_DIR'] = root
        os.environ['TTS_PCM_CACHE_MB'] = '256'
        directory = Path(root) / 'pcm'
        directory.mkdir()
        data = b'\1\0' * 128
        metadata = json.dumps({'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
        for i in range(args.entries):
            (directory / f'{i}.pcm').write_bytes(data)
            (directory / f'{i}.json').write_text(metadata)
        start = time.perf_counter()
        cache = state.PCMCache()
        initialization = time.perf_counter() - start
        for i in range(10):
            assert cache.get(str(i % args.entries)) == data
        start = time.perf_counter()
        for i in range(args.reads):
            assert cache.get(str(i % args.entries)) == data
        elapsed = time.perf_counter() - start
        print(json.dumps({'entries': args.entries, 'reads': args.reads,
                          'initialization_seconds': initialization,
                          'read_seconds': elapsed, 'milliseconds_per_hit': elapsed * 1000 / args.reads}, indent=2))


if __name__ == '__main__':
    main()
