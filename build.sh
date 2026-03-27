#!/bin/bash
# DevShell Linux Build Script

echo "=> Installing NPM dependencies..."
npm install

echo "=> Note: Ensure you have your Python environment ready."
echo "=> Building Electron AppImage for Linux..."
npm run build:linux

echo "=> Done! Check the 'dist' folder for your .AppImage"
