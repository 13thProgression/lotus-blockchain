#!/bin/bash
# Cleans up files/directories that may be left over from previous runs for a clean slate before starting a new build

rm -rf ../venv || true
rm -rf venv || true
rm -rf lotus_blockchain.egg-info || true
rm -rf build_scripts/final_installer || true
rm -rf build_scripts/dist || true
rm -rf build_scripts/pyinstaller || true
rm -rf lotus-blockchain-gui/build || true
rm -rf lotus-blockchain-gui/daemon || true
rm -rf lotus-blockchain-gui/node_modules || true
