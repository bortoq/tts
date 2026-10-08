"""Bounded TXT and streaming FB2 input, without extracting ZIP members."""
import io
import os
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
from tts_voices import normalize_language


def input_limit():
    value = int(os.environ.get('TTS_MAX_BOOK_BYTES', str(64 * 1024 * 1024)))
    if value <= 0:
        raise ValueError('TTS_MAX_BOOK_BYTES must be positive.')
    return value


def parse_fb2(stream):
    lines, language, stack = [], 'ru', []
    blocks = {'p', 'v', 'subtitle', 'text-author', 'date'}
    for event, element in ET.iterparse(stream, events=('start', 'end')):
        tag = element.tag.rsplit('}', 1)[-1]
        if event == 'start':
            stack.append(element)
            continue
        ancestors = [e.tag.rsplit('}', 1)[-1] for e in stack[:-1]]
        if tag == 'lang' and 'title-info' in ancestors and element.text and element.text.strip():
            language = normalize_language(element.text)
        if tag in blocks and 'body' in ancestors:
            line = ''.join(element.itertext()).strip()
            if line:
                lines.append(line)
        # Inline children must survive until their enclosing paragraph is read.
        if not any(name in blocks for name in ancestors):
            element.clear()
            if len(stack) > 1:
                stack[-2].remove(element)
        stack.pop()
    return '\n'.join(lines), language


def read_document(path):
    path = Path(path)
    limit = input_limit()
    if path.stat().st_size > limit:
        raise ValueError(f'Book exceeds TTS_MAX_BOOK_BYTES ({limit} bytes).')
    name = path.name.lower()
    if name.endswith('.fb2.zip'):
        with zipfile.ZipFile(path) as archive:
            books = [item for item in archive.infolist() if item.filename.lower().endswith('.fb2')
                     and not item.filename.startswith('__MACOSX/') and not item.is_dir()]
            if len(books) != 1:
                raise ValueError('ZIP must contain exactly one FB2 file.')
            if books[0].file_size > limit:
                raise ValueError(f'Uncompressed FB2 exceeds TTS_MAX_BOOK_BYTES ({limit} bytes).')
            with archive.open(books[0]) as stream:
                return parse_fb2(stream)
    if name.endswith('.fb2'):
        with path.open('rb') as stream:
            return parse_fb2(stream)
    if not name.endswith('.txt'):
        raise ValueError('Supported formats: TXT, FB2, and FB2.ZIP.')
    with path.open('rb') as stream:
        marker = stream.read(4)
    encodings = ('utf-8-sig', 'utf-16' if marker.startswith((b'\xff\xfe', b'\xfe\xff')) else 'cp1251')
    for encoding in encodings:
        try:
            with path.open('rb') as stream, io.TextIOWrapper(stream, encoding=encoding) as text:
                return text.read().replace('\x00', ''), 'ru'
        except UnicodeDecodeError:
            pass
    raise ValueError('Cannot decode text. Use UTF-8, UTF-16, or Windows-1251.')
