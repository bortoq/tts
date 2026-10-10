"""Bounded TXT and streaming FB2 input, without extracting ZIP members."""
import io
import os
from pathlib import Path
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
import zipfile
from tts_voices import normalize_language


def input_limit():
    value = int(os.environ.get('TTS_MAX_BOOK_BYTES', str(64 * 1024 * 1024)))
    if value <= 0:
        raise ValueError('TTS_MAX_BOOK_BYTES must be positive.')
    return value


class BoundedXMLInput:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.received = stream, limit, 0

    def read(self, size=-1):
        remaining = self.limit - self.received
        data = self.stream.read(min(size, remaining + 1) if size >= 0 else remaining + 1)
        self.received += len(data)
        if self.received > self.limit:
            raise ValueError(f'FB2 exceeds TTS_MAX_BOOK_BYTES ({self.limit} bytes).')
        return data


def parse_fb2(stream):
    lines, language, stack = [], 'ru', []
    limit, text_bytes, elements = input_limit(), 0, 0
    blocks = {'p', 'v', 'subtitle', 'text-author', 'date'}
    for event, element in ET.iterparse(BoundedXMLInput(stream, limit), events=('start', 'end'), forbid_dtd=True):
        tag = element.tag.rsplit('}', 1)[-1]
        if event == 'start':
            stack.append(element)
            elements += 1
            if len(stack) > 128 or elements > 1_000_000:
                raise ValueError('FB2 exceeds XML depth or element limit.')
            continue
        ancestors = [e.tag.rsplit('}', 1)[-1] for e in stack[:-1]]
        if tag == 'lang' and 'title-info' in ancestors and element.text and element.text.strip():
            language = normalize_language(element.text)
        if tag in blocks and 'body' in ancestors:
            line = ''.join(element.itertext()).strip()
            if line:
                text_bytes += len(line.encode('utf-8')) + bool(lines)
                if text_bytes > limit:
                    raise ValueError(f'Extracted text exceeds TTS_MAX_BOOK_BYTES ({limit} bytes).')
                lines.append(line)
        # Inline children must survive until their enclosing paragraph is read.
        if not any(name in blocks for name in ancestors):
            element.clear()
            if len(stack) > 1:
                stack[-2].remove(element)
        stack.pop()
    return '\n'.join(lines), language


def read_document(path):
    try:
        return _read_document(path)
    except DefusedXmlException as error:
        raise ValueError('FB2 DTD and XML entities are forbidden.') from error


def _read_document(path):
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
                result = text.read(limit + 1)
                if len(result) > limit:
                    raise ValueError(f'Extracted text exceeds TTS_MAX_BOOK_BYTES ({limit} bytes).')
                result = result.replace('\x00', '')
                if len(result.encode('utf-8')) > limit:
                    raise ValueError(f'Extracted text exceeds TTS_MAX_BOOK_BYTES ({limit} bytes).')
                return result, 'ru'
        except UnicodeDecodeError:
            pass
    raise ValueError('Cannot decode text. Use UTF-8, UTF-16, or Windows-1251.')
