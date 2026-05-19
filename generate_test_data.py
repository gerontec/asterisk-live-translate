#!/home/gh/python/venv_py311/bin/python3
"""
Generiert WAV-Testdaten für den Integration-Test.
Lädt nur Piper TTS (CPU/ONNX) — kein VRAM-Konflikt mit laufendem Übersetzer.

Ausgabe: test_data/*.wav  (16 kHz, 16-bit mono — matches AudioSocket slin16)
"""
import io, os, sys, wave
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal as sp

PIPER_MODELS_DIR = "/home/gh/python/translator/piper_models"
SR_AS            = 16000
OUT_DIR          = Path(__file__).parent / "test_data"

PIPER_VOICES = {
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-paola-medium",
}

# 3 deutsche Fragen, 4 italienische Antworten
PHRASES = [
    ("de", "q1_de", "Wie geht es Ihnen heute?"),
    ("de", "q2_de", "Können Sie mir bitte helfen?"),
    ("de", "q3_de", "Wann kommt der nächste Zug?"),
    ("it", "a1_it", "Mi sento molto bene, grazie."),
    ("it", "a2_it", "Certamente, posso aiutarla."),
    ("it", "a3_it", "Il prossimo treno arriva tra dieci minuti."),
    ("it", "a4_it", "Prego, non c'è problema."),
]


def load_voice(lang: str):
    from piper.voice import PiperVoice
    path = os.path.join(PIPER_MODELS_DIR, f"{PIPER_VOICES[lang]}.onnx")
    return PiperVoice.load(path)


def synthesise(voice, text: str) -> bytes:
    buf = io.BytesIO()
    wf  = wave.open(buf, "wb")
    voice.synthesize_wav(text, wf)
    wf.close()
    sr_piper = voice.config.sample_rate
    buf.seek(44)
    audio_f = np.frombuffer(buf.read(), dtype=np.int16).astype(np.float32) / 32768.0
    if sr_piper != SR_AS:
        audio_f = sp.resample(audio_f, max(1, int(len(audio_f) * SR_AS / sr_piper)))
    return (audio_f * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


def main():
    OUT_DIR.mkdir(exist_ok=True)
    print("Lade Piper-Stimmen (CPU) …")
    voices = {lang: load_voice(lang) for lang in PIPER_VOICES}
    for lang, model in PIPER_VOICES.items():
        print(f"  {lang}: {model}")

    print("\nSynthese …")
    for lang, name, text in PHRASES:
        pcm   = synthesise(voices[lang], text)
        audio = np.frombuffer(pcm, dtype=np.int16)
        path  = OUT_DIR / f"{name}.wav"
        sf.write(str(path), audio, SR_AS, subtype="PCM_16")
        print(f"  {path.name:20s} {len(audio)/SR_AS:.1f}s  {text!r}")

    print(f"\n{len(PHRASES)} WAVs → {OUT_DIR}/")


if __name__ == "__main__":
    main()
