"""Interruptible synthesis subprocess; stdout contains only signed 16-bit PCM."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

from tts_config import BYTES_PER_SECOND, RATE, executable
from tts_engines import Synthesizer, silero_path
from tts_state import PCMCache, atomic_write, digest
from tts_text import text_parts


def decode_pcm(audio):
    result = subprocess.run([executable('ffmpeg'), '-v', 'error', '-i', str(audio),
                             '-f', 's16le', '-ac', '1', '-ar', str(RATE), 'pipe:1'],
                            capture_output=True, timeout=180)
    if result.returncode or not result.stdout or len(result.stdout) % 2:
        raise RuntimeError('ffmpeg: ' + (result.stderr.decode(errors='replace') or 'empty audio'))
    return result.stdout


def voice_identity(synth):
    identity = {'engine': synth.engine, 'language': synth.language, 'voice': synth.voice,
                'spec': getattr(synth, 'spec', {}), 'pcm': 's16le/24000/mono/v1'}
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
                available = all((cache.directory / (key + '.pcm')).is_file() for key in keys[first:])
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
                pcm = cache.get(key)
                if pcm is None:
                    # Annotation marks can expand the original fragment past the
                    # request limit. Retain its bookmark while splitting requests.
                    pcm = b''.join(decode_pcm(synth.generate(piece, directory))
                                   for piece in text_parts(part, limit, synth.language))
                    cache.put(key, pcm)
                audio = hashlib.sha256(pcm).hexdigest()
                if anchored and index == first:
                    # Regenerated speech may have different timing even within a
                    # fragment: replay that fragment rather than skip unknown words.
                    fraction = cursor['fraction'] if cursor.get('audio') == audio else 0
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
