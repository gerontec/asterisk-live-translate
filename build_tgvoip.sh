#!/usr/bin/env bash
# Build the _tgvoip pybind11 extension against a minimal, DSP-free, callback-audio
# libtgvoip (telegramdesktop fork). No WebRTC DSP, no ALSA/Pulse, system opus/openssl.
set -euo pipefail

PYT=${PYTGVOIP_SRC:-/tmp/pytgvoip}
# Bootstrap the pytgvoip source (binding + telegramdesktop/libtgvoip submodule).
if [ ! -d "$PYT/3rdparty/libtgvoip" ]; then
    echo ">>> cloning pytgvoip source into $PYT ..."
    git clone --recursive https://github.com/bakatrouble/pytgvoip "$PYT"
fi
LIB=$PYT/3rdparty/libtgvoip
SRC=$PYT/src

# The audio callbacks run on libtgvoip's native audio thread, which does NOT hold
# the Python GIL. Old pybind11 tolerated calling Python anyway; pybind11 >=3 aborts
# (PyGILState_Check failure). Acquire the GIL around each Python-touching callback.
if ! grep -q "gil_scoped_acquire _gil" "$SRC/_tgvoip.cpp"; then
    echo ">>> patching _tgvoip.cpp: acquire GIL in audio callbacks ..."
    sed -i \
      -e 's|\(\s*\)char \*frame = this->_send_audio_frame_impl(|\1py::gil_scoped_acquire _gil;\n\1char *frame = this->_send_audio_frame_impl(|' \
      -e 's|\(\s*\)std::string frame((const char \*) buf, sizeof(int16_t) \* size);|\1py::gil_scoped_acquire _gil;\n\1std::string frame((const char *) buf, sizeof(int16_t) * size);|' \
      "$SRC/_tgvoip.cpp"
fi
# Use modern pip pybind11 (bundled 2019 copy is too old for Python 3.11+).
PB=$(/home/gh/python/venv_tgcall/bin/python -c "import pybind11;print(pybind11.get_include())")
OUT=${1:-/home/gh/python/telegram_translate/native}
PYBIN=${2:-/home/gh/python/venv_tgcall/bin/python}

mkdir -p "$OUT/obj"
cd "$LIB"

DEFS="-DTGVOIP_USE_CALLBACK_AUDIO_IO -DTGVOIP_NO_DSP -DWITHOUT_ALSA -DTGVOIP_NO_VIDEO"
INC="-I$LIB -I$LIB/audio $(pkg-config --cflags opus)"
# Newer libstdc++ no longer pulls in <cstdint>/<cstddef> transitively; force them.
CXX="g++ -std=c++17 -O2 -fPIC -w -include cstdint -include cstddef -include cstring"

# libtgvoip core sources (callback audio, no DSP, no device backends, no video)
SRCS="
BlockingQueue.cpp Buffers.cpp CongestionControl.cpp EchoCanceller.cpp
JitterBuffer.cpp MediaStreamItf.cpp MessageThread.cpp NetworkSocket.cpp
OpusDecoder.cpp OpusEncoder.cpp PacketReassembler.cpp
VoIPController.cpp VoIPGroupController.cpp VoIPServerConfig.cpp json11.cpp logging.cpp
audio/AudioIO.cpp audio/AudioIOCallback.cpp audio/AudioInput.cpp
audio/AudioOutput.cpp audio/Resampler.cpp
os/posix/NetworkSocketPosix.cpp
video/ScreamCongestionController.cpp video/VideoRenderer.cpp video/VideoSource.cpp
"

echo ">>> compiling libtgvoip objects ..."
OBJS=""
for s in $SRCS; do
    o="$OUT/obj/$(echo "$s" | tr '/' '_').o"
    echo "  CC $s"
    $CXX $DEFS $INC -c "$s" -o "$o"
    OBJS="$OBJS $o"
done

# Crypto glue (TgVoip.cpp is a mismatched high-level wrapper we skip; it normally
# provides tgvoip::VoIPController::crypto, so we supply it here instead).
echo "  CC voip_crypto.cpp"
$CXX $DEFS $INC -c /home/gh/python/telegram_translate/voip_crypto.cpp -o "$OUT/obj/voip_crypto.o"
OBJS="$OBJS $OUT/obj/voip_crypto.o"

echo ">>> archiving libtgvoip.a ..."
ar rcs "$OUT/libtgvoip.a" $OBJS

echo ">>> building _tgvoip extension ..."
PYINC=$($PYBIN -c "import sysconfig;print(sysconfig.get_paths()['include'])")
EXT=$($PYBIN -c "import sysconfig;print(sysconfig.get_config_var('EXT_SUFFIX'))")
$CXX $DEFS $INC -I"$PB" -I"$PYINC" \
    "$SRC/_tgvoip.cpp" "$SRC/_tgvoip_module.cpp" \
    "$OUT/libtgvoip.a" \
    $(pkg-config --libs opus) -lssl -lcrypto -lpthread -ldl \
    -shared -o "$OUT/_tgvoip$EXT"

echo ">>> built: $OUT/_tgvoip$EXT"
$PYBIN -c "import sys; sys.path.insert(0,'$OUT'); import _tgvoip; print('IMPORT OK', [x for x in dir(_tgvoip) if not x.startswith('__')])"
