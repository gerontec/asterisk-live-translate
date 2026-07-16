#!/usr/bin/env bash
# Cross-compile libtgvoip.a for arm64-v8a (Android / GrapheneOS) with the NDK.
# Callback-audio + no-DSP + no-ALSA — identical config to the x86 build, so no
# OpenSLES device backend is needed. Links against the aarch64 opus + OpenSSL we
# built under android/deps. The pybind11 _tgvoip extension is built on-device in
# Termux against this .a (Termux has the Python headers).
set -euo pipefail

NDK=${NDK:-/home/gh/.buildozer/android/platform/android-ndk-r25b}
API=26
TC=$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin
CXX="$TC/aarch64-linux-android$API-clang++"

PYT=${PYTGVOIP_SRC:-/tmp/pytgvoip}
LIB=$PYT/3rdparty/libtgvoip
DEPS=/home/gh/python/telegram_translate/android/deps
OUT=/home/gh/python/telegram_translate/android/libtgvoip/arm64-v8a
mkdir -p "$OUT/obj"

DEFS="-DTGVOIP_USE_CALLBACK_AUDIO_IO -DTGVOIP_NO_DSP -DWITHOUT_ALSA -DTGVOIP_NO_VIDEO"
INC="-I$LIB -I$LIB/audio -I$DEPS/include -I$DEPS/include/opus"
FLAGS="-std=c++17 -O2 -fPIC -w -include cstdint -include cstddef -include cstring -include sys/system_properties.h"

cd "$LIB"
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
echo ">>> compiling libtgvoip (aarch64) ..."
OBJS=""
for s in $SRCS; do
    o="$OUT/obj/$(echo "$s" | tr '/' '_').o"
    $CXX $DEFS $INC $FLAGS -c "$s" -o "$o"
    OBJS="$OBJS $o"
done
"$TC/llvm-ar" rcs "$OUT/libtgvoip.a" $OBJS
echo ">>> built: $OUT/libtgvoip.a"
file "$OUT/libtgvoip.a"
"$TC/llvm-nm" "$OUT/libtgvoip.a" 2>/dev/null | grep -q "VoIPController" && echo "symbols OK"
