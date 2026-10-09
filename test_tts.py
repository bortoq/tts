import contextlib
import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import tts
import tts_config as config
import tts_engines as engines
import tts_playback as playback
import tts_text


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        # Non-English fixtures check Unicode paths and Windows-1251 decoding.
        self.source = self.directory / "книга с пробелами.txt"
        self.source.write_text("Привет, мир!", encoding="utf-8")

    def test_argument_order_and_default(self):
        self.assertEqual(tts.parse_args([str(self.source)]), (self.source, 1, None, None))
        for mode in config.MODES:
            for args in ([str(self.source), str(mode)], [str(mode), str(self.source)]):
                self.assertEqual(tts.parse_args(args), (self.source, mode, None, None))

    def test_numeric_filename(self):
        source = self.directory / "3"
        source.write_text("test")
        self.assertEqual(tts.parse_args([str(source)]), (source, 1, None, None))
        self.assertEqual(tts.parse_args([str(source), "4"]), (source, 4, None, None))

    def test_invalid_arguments(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for args in (["missing"], [str(self.source), "10"], [str(self.source), "15"],
                         ["1", "2"], [str(self.source), "3", "4"]):
                with self.assertRaises(SystemExit):
                    tts.parse_args(args)

    def test_fb2_encoding_language_and_nested_sections(self):
        source = self.directory / "book.fb2"
        source.write_bytes(('<?xml version="1.0" encoding="windows-1251"?>'
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            '<description><title-info><lang>en-US</lang></title-info></description>'
            '<body><section><title><p>Заголовок</p></title>'
            '<p>Первый <emphasis>абзац</emphasis>.</p>'
            '<section><p>Вложенный.</p></section></section></body>'
            '<body name="notes"><section><p>Примечание.</p></section></body>'
            '<binary>IGNORE</binary></FictionBook>').encode("cp1251"))
        text, lang = tts.read_document(source)
        self.assertEqual(lang, "en-US")
        self.assertEqual(text, "Заголовок\nПервый абзац.\nВложенный.\nПримечание.")

    def test_fb2_zip_without_extraction(self):
        source = self.directory / "book.fb2.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../../book.fb2", '<FictionBook><body><p>Привет</p></body></FictionBook>')
        self.assertEqual(tts.read_document(source), ("Привет", "ru"))
        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("second.fb2", "")
        with self.assertRaises(ValueError):
            tts.read_document(source)

    def test_text_encodings(self):
        for encoding in ("utf-8-sig", "cp1251", "utf-16"):
            self.source.write_bytes("Привет, мир!".encode(encoding))
            self.assertEqual(tts.read_document(self.source), ("Привет, мир!", "ru"))

    def test_split_keeps_every_word_and_bounds_long_tokens(self):
        for text in ("Первое предложение. Второе! " * 100,
                     "Я" * 5000, "слово " * 1000, "", " \n "):
            parts = list(tts_text.text_parts(text))
            self.assertTrue(all(0 < len(p) <= 800 for p in parts))
            self.assertEqual("".join("".join(parts).split()), "".join(text.split()))

    def test_rhvoice_gender_and_language(self):
        voices = self.directory / "voices"
        for name, gender, language in (("Anna", "female", "Russian"),
                                       ("Elena", "female", "Russian"),
                                       ("Aleksandr", "male", "Russian"),
                                       ("Slt", "female", "English"),
                                       ("Leticia", "female", "Brazilian-Portuguese"),
                                       ("Spomenka", "female", "Esperanto")):
            info = voices / name / "voice.info"
            info.parent.mkdir(parents=True)
            info.write_text(f"name={name}\ngender={gender}\nlanguage={language}\n")
        with patch.dict("os.environ", {"TTS_RHVOICE_VOICES": str(voices)}):
            self.assertEqual(engines.choose_rhvoice("ru", "female", tts.MODES[1][2]), "Anna")
            self.assertEqual(engines.choose_rhvoice("ru", "female", "Elena"), "Elena")
            self.assertEqual(engines.choose_rhvoice("ru", "male", "Aleksandr"), "Aleksandr")
            self.assertEqual(engines.choose_rhvoice("en", "female", "unused"), "Slt")
            self.assertEqual(engines.choose_rhvoice("pt", "female", "unused"), "Leticia")
            self.assertEqual(engines.choose_rhvoice("eo", "female", "unused"), "Spomenka")
            with self.assertRaises(ValueError):
                engines.choose_rhvoice("zz", "male", "unused")

    def test_empty_file_is_an_error_before_synthesis(self):
        self.source.write_text(" \n ")
        with patch.object(tts, "read_aloud") as synth, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tts.main([str(self.source)]), 1)
            synth.assert_not_called()

    def test_playback_uses_mpv(self):
        with patch.object(config.shutil, "which", return_value="/usr/bin/mpv") as which:
            command = playback.player_command()
            self.assertEqual(command[0], "/usr/bin/mpv")
            for option in ("--idle=yes", "--input-terminal=yes", "--terminal=yes", "--input-default-bindings=yes"):
                self.assertIn(option, command)
            self.assertNotIn("--really-quiet", command)
            which.assert_called_once_with("mpv")

    def test_missing_mpv_is_an_error(self):
        with patch.object(config.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Program not found: mpv"):
                playback.player_command()

    def test_edge_selects_gender_and_language(self):
        import edge_tts
        voices = [
            {"ShortName": "en-US-Female", "Locale": "en-US", "Gender": "Female"},
            {"ShortName": "en-US-Male", "Locale": "en-US", "Gender": "Male"},
            {"ShortName": "de-DE-Female", "Locale": "de-DE", "Gender": "Female"},
        ]
        with patch.object(edge_tts, "list_voices", new=AsyncMock(return_value=voices)):
            self.assertEqual(asyncio.run(engines.choose_edge("en", "male", "unused")), "en-US-Male")
            self.assertEqual(asyncio.run(engines.choose_edge("en", "female", "unused")), "en-US-Female")
            with self.assertRaises(ValueError):
                asyncio.run(engines.choose_edge("zz", "female", "unused"))



if __name__ == "__main__":
    unittest.main()
