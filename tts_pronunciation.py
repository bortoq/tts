"""Automatic Russian pronunciation preprocessing for Silero."""
import logging
import hashlib
from importlib import metadata, resources
import re

VOWELS = 'аеёиоуыэюяАЕЁИОУЫЭЮЯ'
STRESS = re.compile(r'\+(?=[' + VOWELS + r'])')


def without_annotations(text):
    return STRESS.sub('', text).replace('\u0301', '').replace('ё', 'е').replace('Ё', 'Е')


class SileroPronunciation:
    def __init__(self):
        try:
            from silero_stress import load_accentor
            import torch
        except ImportError as error:
            raise ValueError('For Russian pronunciation preprocessing install its dependencies: python3 -m pip install ".[silero]"') from error
        torch.set_num_threads(2)
        self.accentor = load_accentor()
        model = resources.files('silero_stress.data').joinpath('accentor.pt')
        self.identity = {
            'processor': 'silero-stress', 'version': metadata.version('silero-stress'),
            'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
            'stress_single_vowel': False, 'put_yo_homo': True,
            'markup': 'plus-before-vowel',
            'adapter_revision': 1,
        }

    def __call__(self, text):
        try:
            marked = self.accentor(text, put_stress=True, put_yo=True,
                                   put_stress_homo=True, put_yo_homo=True,
                                   stress_single_vowel=False)
            # A model may add accents and ё, but may never rewrite the book.
            if not isinstance(marked, str) or without_annotations(marked) != without_annotations(text):
                logging.getLogger(__name__).warning("Pronunciation output rejected: unexpected text changes")
                return text
            return marked
        except Exception:
            logging.getLogger(__name__).exception("Pronunciation preprocessing failed; using original text")
            # Preserve original text if inference fails for this fragment.
            return text
