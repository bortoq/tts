"""Shared sentence and clause splitting for speech engines."""
import re

ABBREVIATIONS = {
    'en': {'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs', 'etc', 'e.g', 'i.e'},
    'ru': {'г', 'ул', 'им', 'рис', 'стр', 'проф', 'т.д', 'т.п', 'т.е', 'т.к', 'гг'},
    'de': {'hr', 'fr', 'dr', 'prof', 'bzw', 'ca', 'z.b', 'd.h'},
    'fr': {'mme', 'mlle', 'm', 'dr', 'prof', 'av'},
    'es': {'sr', 'sra', 'srta', 'dr', 'dra', 'prof', 'av'},
    'pt': {'sr', 'sra', 'dr', 'dra', 'prof', 'av', 'etc'},
    'it': {'sig', 'sig.ra', 'dott', 'prof', 'ecc'},
    'uk': {'вул', 'ім', 'рис', 'ст', 'проф', 'т.д', 'т.п'},
}
SENTENCE_END = re.compile(r'''[.!?…]+["'»”’」』】〕〗〙〛）］｝〉》›\)\]]*(?=\s|$)|[。！？]+["'»”’」』】〕〗〙〛）］｝〉》›\)\]]*''')


def sentences(text, language=None):
    """Keep closing quotes and avoid splitting decimals, abbreviations, or initials."""
    text = " ".join(text.replace("\x00", "").split())
    abbreviations = ABBREVIATIONS.get(language.split('-')[0], set()) if language else set().union(*ABBREVIATIONS.values())
    start = 0
    for ending in SENTENCE_END.finditer(text):
        punctuation = ending.group()
        if punctuation.startswith(".") and not punctuation.startswith(".."):
            before = text[max(start, ending.start() - 128):ending.start()]
            word = re.search(r"([\w.]+)$", before)
            after = text[ending.end():ending.end() + 64].lstrip()
            if after and word:
                token = word.group(1)
                if token.casefold() in abbreviations:
                    continue
                if len(token) == 1 and token.isupper() and after[0].isupper():
                    continue
        sentence = text[start:ending.end()].strip()
        if sentence:
            yield sentence
        start = ending.end()
    remainder = text[start:].strip()
    if remainder:
        yield remainder


def text_parts(text, limit=800, language=None):
    """Whole sentences first; long sentences use clauses before word boundaries.

    Google accepts at most 100 characters per request. Do not combine a complete
    sentence with half the next sentence merely to fill that space.
    """
    if limit < 1:
        raise ValueError("The text limit must be positive.")
    pending = ""
    for sentence in sentences(text, language):
        if len(sentence) <= limit:
            combined = pending + " " + sentence if pending else sentence
            if len(combined) <= limit:
                pending = combined
            else:
                yield pending
                pending = sentence
            continue
        if pending:
            yield pending
            pending = ""
        offset = 0
        while len(sentence) - offset > limit:
            window = sentence[offset:offset + limit + 1]
            clauses = list(re.finditer(r'''[,;:—–]["'»”’」』】〕〗〙〛）］｝〉》›\)\]]*\s+''', window))
            cut = clauses[-1].end() if clauses else window.rfind(" ")
            if cut <= 0:
                cut = limit  # A single token exceeds the engine's request limit.
            yield sentence[offset:offset + cut].strip()
            offset += cut
            while offset < len(sentence) and sentence[offset].isspace():
                offset += 1
        if offset < len(sentence):
            yield sentence[offset:]
    if pending:
        yield pending

