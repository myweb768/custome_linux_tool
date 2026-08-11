#!/bin/bash
# =========================================================
# Pop!_OS / COSMIC System Cleanup Script
# Cleans: apt cache, old kernels, journal logs, npm cache,
#         docker unused data, browser cache, thumbnail cache,
#         trash, and shows before/after disk usage.
# Safe to re-run anytime. Does NOT remove personal files,
# LibreOffice, or app configs/settings.
# =========================================================

set -e

echo "=========================================="
echo " Disk usage BEFORE cleanup"
echo "=========================================="
df -h /

echo ""
echo "--- [1/8] Removing unused packages (apt autoremove) ---"
sudo apt autoremove --purge -y

echo ""
echo "--- [2/8] Cleaning apt package cache ---"
sudo apt clean

echo ""
echo "--- [3/8] Trimming system journal logs (keep last 200MB) ---"
sudo journalctl --vacuum-size=200M

echo ""
echo "--- [4/8] Cleaning npm cache ---"
if command -v npm &> /dev/null; then
    npm cache clean --force
else
    echo "npm not found, skipping."
fi

echo ""
echo "--- [5/8] Cleaning unused Docker data (images, containers, build cache) ---"
if command -v docker &> /dev/null && systemctl is-active --quiet docker; then
    docker system prune -f
else
    echo "Docker not running, skipping."
fi

echo ""
echo "--- [6/8] Clearing thumbnail cache ---"
rm -rf ~/.cache/thumbnails/*

echo ""
echo "--- [7/8] Emptying Trash ---"
rm -rf ~/.local/share/Trash/files/*
rm -rf ~/.local/share/Trash/info/*

echo ""
echo "--- [8/8] Clearing browser cache (Brave & Firefox) ---"
# Brave cache (safe: does not touch bookmarks/passwords/history)
find ~/.config/BraveSoftware -type d -iname "*Cache*" -exec rm -rf {} + 2>/dev/null || true
# Firefox cache
find ~/.cache/mozilla -type d -iname "*cache*" -exec rm -rf {} + 2>/dev/null || true

echo ""
echo "=========================================="
echo " Disk usage AFTER cleanup"
echo "=========================================="
df -h /

echo ""
echo "Done. Re-run this script anytime with: bash clean-system.sh"
