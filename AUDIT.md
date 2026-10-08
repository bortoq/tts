# Audit fixes

All eight recommendations from the project audit have been implemented.
The entry point remains `tts.py`, modes are 1–9, and settings use `TTS_*`.
There are no compatibility entry points or aliases. No files in `~/bin` were
inspected or changed.

| Finding | Implemented change | Regression coverage |
| --- | --- | --- |
| Work continued after closing mpv | Dedicated synthesis process, process-group cancellation, reader join, worker-owned partial cleanup | Download/inference/decode cancellation fixtures; real mpv quit during prefill and paused playback |
| Invalid Silero downloads poisoned the cache | Shared atomic downloader; nonempty, length, optional size/hash checks; package validation before commit; invalid cache recovery | Empty response, corrupt cached package, incomplete HTTP body, checksum and reuse tests |
| One transient request stopped a whole book | Four bounded attempts, backoff and Retry-After; validated PCM cache; played-time bookmarks; drain audio before reporting final errors | Retry/status matrix, cache corruption/eviction, cached resume, real RHVoice resume |
| Scripts and regional variants were mismatched | Script/region parsing, conflicting-script rejection, combined Piper local/catalog ranking, reported fallbacks | Chinese/Serbian/Portuguese matrix and exact catalog locale versus installed regional fallback |
| Buffer size depended on arbitrary text pieces | PCM-duration queue, speed-adjusted startup target, keyboard speed updates, slow-producer reporting | Duration and queue bounds at 1x/2.3x/4x; real continuous mpv stream |
| Engines split sentences differently | Shared sentence detector, language-specific abbreviations, Unicode closing delimiters, engine-specific limits | Dialogue, initials, decimals, ellipses, CJK quotes, long tokens, lossless text splitting |
| Any file suffix was decoded as text; ZIP/XML were unbounded | Documented formats only, configurable file/member limit, streaming XML with consumed tree elements removed | PDF rejection, ZIP member limit, large nested-inline FB2 fixture, encodings |
| Responsibilities and dependencies were unclear | Separate input/engine/network/playback/cache modules, pyproject extras and console entry point, portable local wrapper | Existing tests moved to responsible modules; RHVoice/Piper integration and multiple-language coverage |

## Validation and remaining limits

Validation: 70 offline tests passed; Ruff passed; a clean wheel built successfully.
The installed console entry point and a real RHVoice worker from that wheel passed.
The local wrapper also passed after moving it to a directory with spaces.

Offline tests include real mpv terminal controls and real native RHVoice playback
through the production worker. Live RHVoice tests passed for both Russian genders
and English. Live Piper tests passed for both Russian genders. Package tests used temporary directories only.

Live Edge, Google, and Silero model-download checks cannot pass in the restricted
environment: Edge reaches its process deadline, while Google and Silero report
DNS resolution failures. These failures are kept visible; simulated transport and
real-torch PCM tests do not claim live backend verification.

Engine language/gender coverage still depends on upstream voices and installed
models. Unknown gender metadata remains explicitly reported. Region fallback
within a language is reported; a known conflicting script is rejected. A service
that remains slower than playback can still exhaust any finite buffer.

The extracted book text is retained in memory; XML source bytes and a complete
XML tree are no longer retained alongside it. The configurable input limit bounds
this use. Local model reproducibility requires immutable URLs/revisions and expected
checksums; Edge and Google do not expose a stable model revision. Their PCM lookup
uses a daily cache epoch, while voice identity keeps bookmarks usable across days.
