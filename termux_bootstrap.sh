#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# On-device assembly of the DE→EN live-translation phone in Termux on the
# Pixel 8 (Tensor G3) under GrapheneOS. Fully offline, no Google Play Services.
#
# Runs ON THE DEVICE. Prereqs: Termux + Termux:API (F-Droid), and this repo's
# telegram_translate/ folder copied to ~/telegram_translate (incl. the prebuilt
# android/ artifacts: libtgvoip.a + deps + whisper.cpp aarch64 binaries).
#
# Deploy-time script — validate step by step on the device.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
BASE=${ONDEVICE_BASE:-$HOME/telegram_translate}
AND=$BASE/android
cd "$BASE"

echo "== 1. Termux packages =="
pkg update -y
pkg install -y python clang make cmake git binutils libandroid-execinfo \
               openssl libopus wget

echo "== 2. Python deps (Google-free stack) =="
pip install --upgrade pip
pip install "pyrogram==2.0.106" tgcrypto webrtcvad numpy scipy requests pybind11 \
            piper-tts argostranslate

echo "== 3. pytgvoip source + GIL patch (binding only; libtgvoip.a is prebuilt) =="
if [ ! -d pytgvoip ]; then
    git clone --recursive https://github.com/bakatrouble/pytgvoip
fi
SRC=$BASE/pytgvoip/src
grep -q "gil_scoped_acquire _gil" "$SRC/_tgvoip.cpp" || sed -i \
  -e 's|\(\s*\)char \*frame = this->_send_audio_frame_impl(|\1py::gil_scoped_acquire _gil;\n\1char *frame = this->_send_audio_frame_impl(|' \
  -e 's|\(\s*\)std::string frame((const char \*) buf, sizeof(int16_t) \* size);|\1py::gil_scoped_acquire _gil;\n\1std::string frame((const char *) buf, sizeof(int16_t) * size);|' \
  "$SRC/_tgvoip.cpp"

echo "== 4. Build _tgvoip extension against the prebuilt aarch64 libtgvoip.a =="
LIB=$BASE/pytgvoip/3rdparty/libtgvoip
DEPS=$AND/deps
PB=$(python -c 'import pybind11;print(pybind11.get_include())')
PYINC=$(python -c 'import sysconfig;print(sysconfig.get_paths()["include"])')
EXT=$(python -c 'import sysconfig;print(sysconfig.get_config_var("EXT_SUFFIX"))')
DEFS="-DTGVOIP_USE_CALLBACK_AUDIO_IO -DTGVOIP_NO_DSP -DWITHOUT_ALSA -DTGVOIP_NO_VIDEO"
clang++ -std=c++17 -O2 -fPIC -w -include cstdint -include cstddef -include cstring \
    $DEFS -I"$LIB" -I"$LIB/audio" -I"$DEPS/include" -I"$DEPS/include/opus" \
    -I"$PB" -I"$PYINC" \
    "$SRC/_tgvoip.cpp" "$SRC/_tgvoip_module.cpp" \
    "$AND/libtgvoip/arm64-v8a/libtgvoip.a" \
    "$DEPS/lib/libopus.a" "$DEPS/lib/libssl.a" "$DEPS/lib/libcrypto.a" \
    -lpthread -ldl -shared -o "$BASE/_tgvoip$EXT"
cp -r "$SRC/tgvoip" "$BASE/tgvoip"
python -c "import sys;sys.path.insert(0,'$BASE');import _tgvoip;print('_tgvoip import OK')"

echo "== 5. Models (offline) =="
mkdir -p "$BASE/models"
# Whisper ggml small (multilingual) — ~488 MB; use base for faster/smaller.
[ -f "$BASE/models/ggml-small.bin" ] || wget -O "$BASE/models/ggml-small.bin" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin
# Piper English voice
[ -f "$BASE/models/en_GB-alan-medium.onnx" ] || wget -O "$BASE/models/en_GB-alan-medium.onnx" \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx
[ -f "$BASE/models/en_GB-alan-medium.onnx.json" ] || wget -O "$BASE/models/en_GB-alan-medium.onnx.json" \
  https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB/alan/medium/en_GB-alan-medium.onnx.json
# Argos Translate de→en package
python - <<'PY'
import argostranslate.package as p
p.update_package_index()
avail = p.get_available_packages()
pkg = next(x for x in avail if x.from_code == "de" and x.to_code == "en")
p.install_from_path(pkg.download())
print("Argos de→en installed")
PY

chmod +x "$AND/whisper/arm64-v8a/whisper-cli" || true

cat <<EOF

== Done. To run (two Termux sessions or use tmux) ==
  export LD_LIBRARY_PATH=$AND/whisper/arm64-v8a:\$LD_LIBRARY_PATH
  python $BASE/inference_server_ondevice.py        # local :9095
  python $BASE/telegram_translate_bot.py           # INFER already 127.0.0.1:9095

First run of telegram_translate_bot.py will do the Telegram login (phone + code).
EOF
