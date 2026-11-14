#!/bin/sh
set -eu
cd "$(dirname "$0")"
export RUSTUP_HOME="$PWD/toolchain/rustup"
export CARGO_HOME="$PWD/toolchain/cargo"
export CARGO_TARGET_DIR="$PWD/target"
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-1}"
export PATH="$CARGO_HOME/bin:$PATH"
if [ ! -x "$CARGO_HOME/bin/cargo" ]; then
    mkdir -p toolchain
    curl --fail --show-error --location https://sh.rustup.rs -o toolchain/rustup.sh
    sh toolchain/rustup.sh -y --profile minimal --default-toolchain 1.100.0 --no-modify-path
fi
rustc --version
cargo build --locked --release --manifest-path receiver/Cargo.toml
cargo test --locked --release --manifest-path vendor/FusionPlay.MiPlaySdk/Cargo.toml --no-default-features --features headless
mkdir -p bin
cp target/release/miair-miplay-receiver bin/miair-miplay-receiver
