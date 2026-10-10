"""Shared configuration and executable lookup."""
import os
import shutil

RATE = 24000
BYTES_PER_SECOND = RATE * 2

MODES = {
    1: ("RHVoice", "female", "Anna"),
    2: ("RHVoice", "male", "Aleksandr"),
    3: ("Edge", "female", "ru-RU-SvetlanaNeural"),
    4: ("Edge", "male", "ru-RU-DmitryNeural"),
    5: ("Silero", "female", "xenia"),
    6: ("Silero", "male", "aidar"),
    7: ("Piper", "female", "ru_RU-irina-medium"),
    8: ("Piper", "male", "ru_RU-dmitri-medium"),
    9: ("Google", "female", "auto"),
}


def network_timeout():
    value = float(os.environ.get("TTS_NETWORK_TIMEOUT", "60"))
    if not 0 < value <= 3600:
        raise ValueError("TTS_NETWORK_TIMEOUT must be greater than 0 and at most 3600 seconds.")
    return value


def executable(name, fallback=None):
    found = shutil.which(name)
    if found:
        return found
    if fallback and fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise ValueError(f"Program not found: {name}")


def byte_limit(name, default):
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f'{name} must be positive.')
    return value


