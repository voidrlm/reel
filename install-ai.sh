#!/usr/bin/env bash
# Builds whisper.cpp's command-line tool for Reel's "Generate with AI" subtitles.
# It is compiled for this computer's CPU (AVX2/AVX-512/NEON...), so it runs as fast as the machine allows.
# Missing build tools are installed through the system's package manager (asks for the password once).
# Result: ~/.local/share/reel/bin/whisper-cli. Reel downloads the right model itself when first used.
set -euo pipefail
TAG=v1.8.7
DEST="${REEL_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/reel}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

need=()
command -v g++ >/dev/null || need+=(compiler)
command -v git >/dev/null || need+=(git)
command -v cmake >/dev/null || command -v uvx >/dev/null || need+=(cmake)
if [ ${#need[@]} -gt 0 ]; then
    echo "STEP Installing build tools (${need[*]})"
    if command -v dnf >/dev/null; then pkgs=(gcc-c++ git cmake); cmd=(dnf install -y)
    elif command -v apt-get >/dev/null; then pkgs=(g++ git cmake); cmd=(apt-get install -y)
    elif command -v pacman >/dev/null; then pkgs=(gcc git cmake); cmd=(pacman -S --needed --noconfirm)
    elif command -v zypper >/dev/null; then pkgs=(gcc-c++ git cmake); cmd=(zypper --non-interactive install)
    else echo "Please install a C++ compiler, git and cmake, then try again." >&2; exit 1; fi
    if [ "$(id -u)" = 0 ]; then "${cmd[@]}" "${pkgs[@]}"
    elif command -v pkexec >/dev/null; then pkexec "${cmd[@]}" "${pkgs[@]}"
    else echo "Please run: sudo ${cmd[*]} ${pkgs[*]}" >&2; exit 1; fi
fi
if command -v cmake >/dev/null; then CMAKE=(cmake); else CMAKE=(uvx --from cmake cmake); fi

echo "STEP Downloading the AI engine (whisper.cpp $TAG)"
git clone --quiet --depth 1 --branch "$TAG" https://github.com/ggml-org/whisper.cpp "$WORK/src"
echo "STEP Building the AI engine for this computer"
"${CMAKE[@]}" -S "$WORK/src" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
    -DWHISPER_BUILD_TESTS=OFF -DWHISPER_SDL2=OFF -DGGML_NATIVE=ON >/dev/null
"${CMAKE[@]}" --build "$WORK/build" -j"$(nproc)" --target whisper-cli 2>&1 |
    sed -un 's/^\[ *\([0-9]*\)%\].*/PROGRESS \1/p'
mkdir -p "$DEST/bin"
install -m755 "$WORK/build/bin/whisper-cli" "$DEST/bin/whisper-cli"
echo "DONE $DEST/bin/whisper-cli"
