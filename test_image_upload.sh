#!/bin/bash

# Test wheel image upload functionality
# Usage: ./test_image_upload.sh [wheel_slug] [image_file_path]

WHEEL=${1:-ccw}
IMAGE_FILE=${2:-/tmp/test.png}

echo "=== Wheel Image Upload Test ==="
echo "Wheel: $WHEEL"
echo "Image: $IMAGE_FILE"
echo ""

# Check if image file exists
if [ ! -f "$IMAGE_FILE" ]; then
  echo "✗ Image file not found: $IMAGE_FILE"
  echo ""
  echo "Creating a test PNG image..."
  python3 << 'EOF'
import base64
import sys

# Minimal 1x1 transparent PNG
png_bytes = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
    0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41,
    0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
    0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
    0x42, 0x60, 0x82
])

with open('/tmp/test.png', 'wb') as f:
    f.write(png_bytes)
print("✓ Test image created: /tmp/test.png")
EOF
  IMAGE_FILE=/tmp/test.png
fi

# Convert image to base64
echo "Reading image..."
BASE64=$(base64 < "$IMAGE_FILE" | tr -d '\n')
echo "✓ Image converted to base64 (${#BASE64} chars)"
echo ""

# Get file extension
EXT=".${IMAGE_FILE##*.}"
echo "File extension: $EXT"
echo ""

# Upload via API
echo "Uploading to Flask API..."
RESPONSE=$(curl -s -X POST 'http://localhost:8420/api/wheel-image' \
  -H 'Content-Type: application/json' \
  -d '{
    "wheel": "'$WHEEL'",
    "data": "'$BASE64'",
    "ext": "'$EXT'"
  }')

echo "Response: $RESPONSE"
echo ""

# Check if upload was successful
if echo "$RESPONSE" | grep -q '"ok":true'; then
  echo "✓ Upload successful!"

  # Verify file exists
  if [ -f "wheels/$WHEEL/image$EXT" ]; then
    echo "✓ File saved to: wheels/$WHEEL/image$EXT"
    ls -lh "wheels/$WHEEL/image$EXT"
  fi

  echo ""
  echo "Next steps:"
  echo "1. If running Astro dev server, restart it: cd site && npm run dev"
  echo "2. Visit http://localhost:4321 to see the image on the landing page"
else
  echo "✗ Upload failed!"
  exit 1
fi
