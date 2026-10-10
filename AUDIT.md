# External audit fixes — 2026-10-09

This document records changes made after reviewing the external audit of commit
`2e8d198f63dae0dcdd9cb9433db741a34b5f165e`. It replaces the earlier audit summary,
which contained stale claims about daily online-cache invalidation and unspecified
lint checks. These results concern code, packaging and signal generation; they
are not a listening assessment or a penetration test of speech providers.

| Finding | Change | Regression coverage |
| --- | --- | --- |
| New PCM could be evicted when timestamps tied | A SQLite logical access sequence is updated under the existing cross-process file lock. Newly written PCM is excluded from eviction candidates. Existing PCM metadata remains compatible. | Identical file timestamps, access after insertion, independent cache instances, concurrent processes |
| XML entities bypassed the input limit | Declared dependency `defusedxml==0.7.1`; reject all DTDs/entities; enforce actual XML input bytes and extracted UTF-8 text bytes, maximum depth 128 and one million elements. TXT output is also bounded in UTF-8 bytes. | Plain/zipped entity fixtures, DTD without entities, nested duplicate text, excessive depth, existing encodings and inline markup |
| Silero executed packages before a mandatory fingerprint check | Nine built-in model sizes/SHA-256 values in `tts_model_pins.py`; default catalog pinned to commit `d9355348e2781dc8fa25a135d1602c530afae24c` and SHA-256. Reject automatic unpinned packages. Explicit unpinned voice overrides are treated as trusted configuration and warned about in the terminal before worker startup, as well as before package loading. | Wrong cached/downloaded hashes never reach PackageImporter; unknown unpinned model rejected; warnings precede loading; adding verification metadata preserves bookmarks for unchanged model bytes |
| ISO aliases depended on system files | Ship the ISO 639-2 terminology/bibliographic codes and English names in `tts_languages.py`; no `/usr/share/iso-codes` lookup. Three-letter-only codes remain unchanged. | `fra/fre`, `deu/ger`, `zho/chi`, `ces/cze`, script tags and language names without system data |
| Responses and PCM were unbounded | Byte ceilings on RPC/catalogs, downloads, streamed Edge audio and generated audio. HTTP opening has a bounded wait including DNS/headers; complete response bodies share the attempt's deadline. ffmpeg pipes are drained incrementally with bounded PCM/diagnostics and a 180-second wall deadline; combined fragments are bounded too. | Declared/actual oversized replies, failed download cleanup, trickled-body deadline, stalled opening and late-response close, Edge stream stop, real ffmpeg output limit |
| Base install tests failed on optional imports; no quality gate | Individual tests explicitly skip absent extras. GitHub Actions base matrix Python 3.10–3.13 plus Edge/Silero on 3.11; mpv/ffmpeg, tests, lint, build and installed-wheel smoke check. Separate manually dispatched live synthesis workflow. Ruff, build, setuptools and wheel versions pinned. | Full local suite and clean base virtual environment; installed wheel tested outside checkout |
| No distribution license | MIT license and SPDX packaging metadata; external models/engines retain their own licenses. | Built wheel includes LICENSE |
| Audit/documentation drift | Updated README resource settings, model trust boundary, ISO data, quality-gate commands and cache lifetime; replaced this audit summary. | Online PCM calendar-change regression remains enabled |

## Validation

Commands executed with Python **3.11.4** on Linux:

```bash
python3 -m unittest discover -v
python3 -m ruff check .
python3 -m build --outdir /tmp/tts-audit-dist
python3 integration_tests.py --engine all --timeout 30
```

- Full installed environment: **102 tests passed**, no skips. This includes real
  mpv PTY/control tests and native RHVoice playback; these are not mocked player
  checks.
- Clean base virtual environment, installed with `.[dev]`, without Edge, Torch
  or PyYAML: **87 passed, 15 explicitly skipped, no failures/errors**. Skipped
  optional-backend tests are not counted as verified.
- **Ruff 0.16.10**: `ruff check .` passed with the repository's explicit
  `E4,E7,E9,F` policy. `--isolated` intentionally ignores that policy and is not
  the documented lint command.
- Wheel and source archive built with **setuptools 80.9.0 / wheel 0.45.1**.
  The wheel was installed in an isolated environment and its CLI, ISO aliases,
  XML parsing and model-pin module checked from `/tmp`, outside the checkout.
- **10 real synthesis tests passed**: RHVoice Russian female/male and English,
  Edge female/male, Silero female/male, Piper female/male, Google. Tests decode
  the generated files and check duration/signal level without playing sound.
  Silero used the verified cached Russian model; its pinned bytes were also
  freshly downloaded from the official server to `/tmp` and fingerprinted.
- All nine pinned model files and the immutable catalog were downloaded and
  fingerprinted without executing their packages. The installed Russian model
  has the same SHA-256 as the freshly downloaded pinned file.
- `git diff --check` passed.

The GitHub Actions definitions were added locally; their remote matrix has not
been run as part of this local validation. Local checks used Python 3.11, not all
four CI interpreter versions. Live synthesis checks validate signal generation,
not pronunciation, absence of acoustic artifacts, or voice quality.

## Remaining product and trust boundaries

Silero's `torch.package` loading can execute Python code. SHA-256 pins prevent
accepting changed package bytes; they do not sandbox trusted upstream code.
Explicit unpinned overrides retain this risk and display a warning. Updating
model pins requires a separate upstream review and fingerprint update. Additional
catalog models need a known SHA-256 or an explicit trusted voice configuration.

A timed-out HTTP opener may retain a daemon thread while the system completes
DNS/header I/O. It cannot return a late response to the caller or model validator;
late responses are closed. Per-attempt deadlines do not include retry backoff:
there can be four attempts and bounded Retry-After waits. Downloads, audio and
book text remain finite but model inference still needs enough RAM for the
selected model. Limits are documented and configurable in README.

The reader targets Linux/POSIX (`fcntl`, process groups, mpv Lua/terminal).
RHVoice/Piper require their external programs and voices. Edge/Google send text
to their providers, whose voice revisions cannot be pinned. Generated online PCM
persists until size-based LRU eviction; it has no daily expiration.
