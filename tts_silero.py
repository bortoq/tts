"""Pre-vocoder pause correction for the pinned Silero v4_ru package."""

MEL_PAUSE_REVISION = 'silero-mel-pauses-v1'
PUNCTUATION = '.,!?;:–…'


def quiet_pause_mel(mel, durations, word_mask, sequence, pause_ids, silence):
    """Replace the protected middle of long non-word pauses, retaining time.

    A 125 ms left guard and 150 ms right guard retain acoustic transitions.
    Blend the spectrum for 25 ms at both edges. Neither amplitude nor a VAD
    decides what to mute: a qualifying run must contain a space or punctuation token.
    """
    import torch
    lengths = durations.detach().cpu().reshape(-1).tolist()
    tokens = sequence.detach().cpu().reshape(-1).tolist()
    if len(lengths) != len(word_mask) or len(tokens) != len(lengths):
        raise ValueError('Silero returned inconsistent pause alignment.')
    if any(value < 0 or not float(value).is_integer() for value in lengths):
        raise ValueError('Silero returned an invalid token duration.')
    if mel.ndim != 3 or mel.shape[0] != 1 or sum(lengths) != mel.shape[-1]:
        raise ValueError('Silero spectrum does not match token durations.')
    result, position, start, is_pause = None, 0, None, False
    # Model acoustic frames are 12.5 ms; guards/fades are in that same clock.
    for index in range(len(lengths) + 1):
        spoken = index == len(lengths) or bool(word_mask[index])
        if not spoken:
            if start is None:
                start = position
            is_pause |= tokens[index] in pause_ids
        elif start is not None:
            left, right = int(start) + 10, int(position) - 12
            if is_pause and right - left >= 6:  # At least 350 ms total.
                if result is None:
                    result = mel.clone()
                weight = torch.ones(right - left, dtype=mel.dtype, device=mel.device)
                weight[:2] = torch.tensor([1 / 3, 2 / 3], dtype=mel.dtype, device=mel.device)
                weight[-2:] = weight[:2].flip(0)
                source = mel[:, :, left:right]
                result[:, :, left:right] = source + (silence - source) * weight
            start, is_pause = None, False
        if index < len(lengths):
            position += lengths[index]
    return mel if result is None else result


class SileroV4PauseAdapter:
    """Reuse pinned model components and correct mel before its sole vocoder call.

    The reader supplies plain text, not SSML. This adapter implements that path
    only, preserving upstream accentuation, duration and pitch prediction.
    """
    def __init__(self, model):
        self.model = model

    def generate(self, text, speaker, sample_rate, *, reference=False):
        import torch
        if sample_rate not in (8000, 24000, 48000):
            raise ValueError('Silero v4_ru supports 8000, 24000 or 48000 Hz.')
        model = self.model
        speaker_ids, model_id = model.get_speakers(speaker)
        sentences, clean, breaks, rates, pitches, speakers = model.prepare_tts_model_input(
            text, ssml=False, speaker_ids=speaker_ids)
        if not model.q_model_unpacked:
            model.unpack_q_model()
            model.q_model_unpacked = True
        system = model.models[model_id]
        accented = []
        for sentence, clean_sentence in zip(sentences, clean):
            raw, tokens, prediction_mask = system._tokenize_clean(sentence, clean_sentence)
            probs, predictions, yo_probs, yo_predictions = system._get_model_preds(tokens)
            marked = system.postprocess_accentor(raw, tokens, prediction_mask, probs,
                                                predictions, yo_probs, yo_predictions, True)
            accented.append(system._fuse_words_to_sentence(marked).lower())
        sequence, forced, rate, pitch_coefs, word_mask = system.merge_batch_model(
            accented, breaks, rates, pitches)
        if forced:
            raise ValueError('The Silero pause adapter only supports plain text.')
        tts = system.tts_model
        padding = sequence.eq(0)
        durations = torch.round((torch.exp(tts.dur_predictor(sequence, speakers, padding, 1.))
                                 - 1).clamp(min=0))
        durations = torch.round(durations / rate)
        pitch = tts.update_pitch_coef(tts.pitch_predictor(sequence, speakers, padding, 1.),
                                     pitch_coefs, speakers)
        mel = tts.tacotron(sequence, speakers, padding, durations, pitch)
        pause_symbols = {model.symb_to_ascii(symbol) for symbol in PUNCTUATION + ' '}
        pause_ids = {system.symbol_to_id[symbol] for symbol in pause_symbols
                     if symbol in system.symbol_to_id}
        corrected = quiet_pause_mel(mel, durations, word_mask, sequence,
                                    pause_ids, tts.sil_value)
        audio = tts.vocoder(corrected, sample_rate, 0., True).detach().cpu().reshape(-1)
        original = None
        if reference:
            original = (audio if corrected is mel else
                        tts.vocoder(mel, sample_rate, 0., True).detach().cpu().reshape(-1))
        return audio, original
