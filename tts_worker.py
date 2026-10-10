"""Interruptible synthesis subprocess; stdout contains only signed 16-bit PCM."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time

from tts_config import BYTES_PER_SECOND, RATE, byte_limit, executable
from tts_engines import Synthesizer, silero_path
from tts_state import PCMCache, atomic_write, digest
from tts_text import text_parts
from tts_silero import MEL_PAUSE_REVISION


def decode_pcm(audio):
    maximum = byte_limit('TTS_MAX_PCM_BYTES', 180 * BYTES_PER_SECOND)
    with subprocess.Popen([executable('ffmpeg'), '-v', 'error', '-i', str(audio),
                           '-f', 's16le', '-ac', '1', '-ar', str(RATE), 'pipe:1'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        pcm, errors = bytearray(), bytearray()
        deadline = time.monotonic() + 180
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, pcm)
                selector.register(process.stderr, selectors.EVENT_READ, errors)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('ffmpeg exceeded its total deadline.')
                    for key, _ in selector.select(min(remaining, 0.1)):
                        block = os.read(key.fd, 64 * 1024)
                        if not block:
                            selector.unregister(key.fileobj)
                        elif key.data is pcm:
                            if len(pcm) + len(block) > maximum:
                                raise ValueError(f'ffmpeg output exceeds TTS_MAX_PCM_BYTES ({maximum} bytes).')
                            pcm.extend(block)
                        elif len(errors) < 64 * 1024:
                            errors.extend(block[:64 * 1024 - len(errors)])
            status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
    if status or not pcm or len(pcm) % 2:
        raise RuntimeError('ffmpeg: ' + (errors.decode(errors='replace') or 'empty audio'))
    return bytes(pcm)


def voice_identity(synth):
    # Verification metadata does not change voice/audio. Keep bookmarks for
    # existing pinned bytes; model_sha256 below identifies actual model changes.
    spec = {key: value for key, value in getattr(synth, 'spec', {}).items()
            if key not in ('size_bytes', 'md5_digest', 'sha256_digest', 'trusted_override')}
    identity = {'engine': synth.engine, 'language': synth.language, 'voice': synth.voice,
                'spec': spec, 'pcm': 's16le/24000/mono/v1'}
    packages = {'Edge': ('edge-tts',), 'Silero': ('torch', 'aksharamukha')}
    for package in packages.get(synth.engine, ()):
        try:
            identity[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    if synth.engine in ('Silero', 'Piper'):
        model = Path(synth.model) if synth.engine == 'Piper' else silero_path(synth.spec)
        hasher = hashlib.sha256()
        with model.open('rb') as stream:
            while block := stream.read(1024 * 1024):
                hasher.update(block)
        identity['model_sha256'] = hasher.hexdigest()
        if synth.engine == 'Piper':
            identity['config_sha256'] = hashlib.sha256(Path(str(model) + '.json').read_bytes()).hexdigest()
    if synth.engine == 'RHVoice':
        from tts_voices import rhvoice_directories
        identity['binary'] = [synth.binary, Path(synth.binary).stat().st_size,
                              Path(synth.binary).stat().st_mtime_ns]
        identity['voice_files'] = []
        for directory in rhvoice_directories():
            for info in directory.glob('*/voice.info'):
                fields = dict(line.split('=', 1) for line in info.read_text().splitlines() if '=' in line)
                if fields.get('name') == synth.voice:
                    identity['voice_files'].extend((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                                   for p in sorted(info.parent.rglob('*')) if p.is_file())
    return digest(identity)


def continuous_rhvoice(synth, text, directory):
    source = directory / 'text.txt'
    source.write_text(text, encoding='utf-8')
    with (directory / 'rhvoice.log').open('wb') as errors:
        producer = subprocess.Popen([synth.binary, '-p', synth.voice, '-i', str(source), '-o', '-'],
                                    stdout=subprocess.PIPE, stderr=errors)
        decoder = subprocess.Popen([executable('ffmpeg'), '-v', 'error', '-i', 'pipe:0',
                                    '-f', 's16le', '-ac', '1', '-ar', str(RATE), 'pipe:1'],
                                   stdin=producer.stdout, stdout=subprocess.PIPE, stderr=errors)
        producer.stdout.close()
        try:
            count = 0
            while block := decoder.stdout.read(BYTES_PER_SECOND):
                count += len(block)
                yield block
            if not count:
                raise RuntimeError("RHVoice generated no audio.")
            if decoder.wait() or producer.wait():
                raise RuntimeError((directory / 'rhvoice.log').read_text(errors='replace'))
        finally:
            decoder.stdout.close()
            for process in (decoder, producer):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def produce(job, directory, output=None):
    output = output or sys.stdout.buffer
    text = Path(job['text_file']).read_text(encoding='utf-8')
    synth = Synthesizer(job['mode'], job['language'])
    identity = voice_identity(synth)
    accentor = None
    if synth.language.split("-")[0] == "ru" and synth.engine == "Silero":
        from tts_pronunciation import SileroPronunciation
        accentor = SileroPronunciation()
        identity = digest([identity, accentor.identity])
    resume = job['bookmark']
    seconds = resume['seconds'] if resume['voice'] == identity else 0
    atomic_write(directory / 'voice.json', json.dumps({'voice': identity, 'offset': seconds,
                 'engine': synth.engine, 'name': synth.voice}).encode())
    skip = int(seconds * RATE) * 2
    def emit(data):
        nonlocal skip
        cut = min(len(data), skip)
        skip -= cut
        if cut < len(data):
            output.write(data[cut:])
            output.flush()
    cache = PCMCache()
    if synth.engine == 'RHVoice':
        # Synthesize the entire book as one utterance, retaining native continuity.
        # Reuse complete cached streams; otherwise regenerate and skip the prefix.
        book_key = digest([identity, text])
        manifest = cache.directory / (book_key + '.book.json')
        try:
            keys = json.loads(manifest.read_text())
            available = isinstance(keys, list) and bool(keys) and all(isinstance(key, str) for key in keys)
            if available:
                first = min(int(seconds), len(keys) - 1)
                maximum = byte_limit('TTS_MAX_PCM_BYTES', 180 * BYTES_PER_SECOND)
                available = all((cache.directory / (key + '.pcm')).is_file()
                                and (cache.directory / (key + '.pcm')).stat().st_size <= maximum
                                for key in keys[first:])
        except (OSError, ValueError):
            available = False
        if available:
            # Native blocks are one audio second each (except the last).
            first = min(int(seconds), len(keys) - 1)
            skip -= first * BYTES_PER_SECOND
            for key in keys[first:]:
                block = cache.get(key)
                if block is None:
                    manifest.unlink(missing_ok=True)
                    raise RuntimeError('PCM cache changed during playback; restart to resume.')
                emit(block)
        else:
            manifest.unlink(missing_ok=True)
            keys, cached_bytes = [], 0
            eligible = bool(cache.limit)
            for index, block in enumerate(continuous_rhvoice(synth, text, directory)):
                key = digest([book_key, index])
                cache.put(key, block)
                cached_bytes += len(block)
                if eligible and cached_bytes <= cache.limit:
                    keys.append(key)
                else:
                    eligible = False
                    keys.clear()
                emit(block)
            if eligible and all((cache.directory / (key + '.pcm')).is_file() for key in keys):
                atomic_write(manifest, json.dumps(keys).encode())
    else:
        limit = 100 if synth.engine == 'Google' else 800
        adapted_silero = getattr(synth, 'processing_revision', None) == MEL_PAUSE_REVISION
        # Original fragments identify the reading location independently of audio
        # duration and pronunciation annotations. Keep layout stable across sessions.
        parts = list(text_parts(text, limit, synth.language))
        layout = digest([parts, 'text-fragments-v1'])
        cursor = resume.get('cursor', {}) if resume['voice'] == identity else {}
        anchored = (cursor.get('layout') == layout and type(cursor.get('index')) is int
                    and 0 <= cursor['index'] < len(parts)
                    and isinstance(cursor.get('fraction'), (int, float))
                    and 0 <= cursor['fraction'] <= 1)
        first = cursor['index'] if anchored else 0
        if anchored:
            skip = 0
        delivered = 0
        with (directory / 'segments.jsonl').open('w') as timeline:
            for index in range(first, len(parts)):
                part = accentor(parts[index]) if accentor else parts[index]
                # Persist online audio until normal LRU eviction; calendar changes
                # must not invalidate a reading session's audio.
                key = digest([identity, part, None])
                if adapted_silero:
                    pcm, processing = cache.get(key, with_processing=True)
                else:
                    pcm, processing = cache.get(key), None
                legacy, legacy_processing = pcm, processing
                ready = (isinstance(processing, dict)
                         and processing.get('revision') == MEL_PAUSE_REVISION
                         and isinstance(processing.get('compatible_audio'), list))
                if adapted_silero and not ready:
                    # Regenerate audio from older pause treatments once.
                    pcm = None
                needs_cache = pcm is None
                if pcm is None:
                    # Annotation marks can expand the original fragment past the
                    # request limit. Retain its bookmark while splitting requests.
                    blocks, reference_blocks, total = [], [], 0
                    reference_needed = adapted_silero and (legacy is not None or anchored and index == first)
                    for piece in text_parts(part, limit, synth.language):
                        if adapted_silero:
                            synth.reference_requested = reference_needed
                        block = decode_pcm(synth.generate(piece, directory))
                        total += len(block)
                        if total > byte_limit('TTS_MAX_PCM_BYTES', 180 * BYTES_PER_SECOND):
                            raise ValueError('Combined fragment exceeds TTS_MAX_PCM_BYTES.')
                        if reference_needed:
                            if synth.reference_file is None:
                                raise ValueError('Silero did not return reference audio.')
                            reference_blocks.append(decode_pcm(synth.reference_file))
                        blocks.append(block)
                    pcm = b''.join(blocks)
                    if adapted_silero:
                        compatible = []
                        if reference_needed:
                            raw = b''.join(reference_blocks)
                            raw_hash = hashlib.sha256(raw).hexdigest()
                            compatible.append(raw_hash)
                            old_aliases = (legacy_processing.get('compatible_audio', [])
                                           if isinstance(legacy_processing, dict) else [])
                            if not isinstance(old_aliases, list):
                                old_aliases = []
                            if (legacy is not None and len(legacy) == len(pcm) == len(raw)
                                    and (hashlib.sha256(legacy).hexdigest() == raw_hash
                                         or raw_hash in old_aliases)):
                                compatible.append(hashlib.sha256(legacy).hexdigest())
                                compatible.extend(value for value in old_aliases if isinstance(value, str))
                        processing = {'revision': MEL_PAUSE_REVISION,
                                      'compatible_audio': sorted(set(compatible))}
                if needs_cache:
                    if adapted_silero:
                        cache.put(key, pcm, processing=processing)
                    else:
                        cache.put(key, pcm)
                audio = hashlib.sha256(pcm).hexdigest()
                if anchored and index == first:
                    # Regenerated speech may have different timing even within a
                    # fragment: replay that fragment rather than skip unknown words.
                    unchanged_timing = (cursor.get('audio') == audio
                        or adapted_silero and cursor.get('audio') in processing['compatible_audio'])
                    fraction = cursor['fraction'] if unchanged_timing else 0
                    cut = min(len(pcm), int(len(pcm) / 2 * fraction) * 2)
                else:
                    cut = min(len(pcm), skip)
                    skip -= cut
                if cut == len(pcm):
                    continue
                timeline.write(json.dumps({'index': index, 'layout': layout,
                    'start': delivered / BYTES_PER_SECOND, 'cut': cut / BYTES_PER_SECOND,
                    'duration': len(pcm) / BYTES_PER_SECOND, 'audio': audio}) + '\n')
                timeline.flush()
                output.write(pcm[cut:])
                output.flush()
                delivered += len(pcm) - cut


def main():
    directory = Path(sys.argv[1])
    try:
        import os
        # Keep library diagnostics out of the PCM stream and the terminal.
        with os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0) as output, \
             (directory / 'synthesis.log').open('w') as diagnostics:
            os.dup2(diagnostics.fileno(), sys.stdout.fileno())
            os.dup2(diagnostics.fileno(), sys.stderr.fileno())
            produce(json.loads((directory / 'job.json').read_text()), directory, output)
        return 0
    except Exception as error:
        atomic_write(directory / 'error.txt', str(error).encode())
        return 1
    finally:
        try:
            log = directory / 'synthesis.log'
            if log.exists() and log.stat().st_size:
                from tts_voices import cache_dir
                with log.open('rb') as source:
                    source.seek(max(0, log.stat().st_size - 1024 * 1024))
                    atomic_write(cache_dir() / 'logs' / ('synthesis-' + directory.name + '.log'), source.read())
                # Bound diagnostic history independently of the audio cache.
                logs = sorted((cache_dir() / 'logs').glob('synthesis-*.log'), key=lambda p: p.stat().st_mtime)
                for old in logs[:-10]:
                    old.unlink(missing_ok=True)
        except OSError:
            # A failed diagnostic write must not replace the synthesis result.
            pass


if __name__ == '__main__':
    sys.exit(main())
