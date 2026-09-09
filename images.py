"""Image re-encoding helpers for destination size caps.

Destinations enforce their own image size limits and the posters'
degrade-to-text-only contract means an oversized image silently produces a
text-only post (2026-09-09 glass.photo report: ~2.9 MB source JPEGs vs
Bluesky's blob cap — every glass photo dropped on Bluesky). Instead of
dropping, oversized images are downscaled/re-encoded to fit, mirroring what
Bluesky's own client does before upload.

Pillow is a lazy import inside the functions: a broken or missing install
degrades to "no downscale" (the old skip behavior) rather than breaking
dispatch, and keeps `import images` cheap on paths that never touch images.
"""

import logging

logger = logging.getLogger("feedecho.images")

# Target ceiling after re-encode. Sits below Bluesky's 2 MB app-level image
# limit so one quality pass has headroom to converge without looping.
DOWNSCALE_TARGET_BYTES = 2 * 1024 * 1024

# Presets tried in order; first one that fits wins. Tuned for JPEG photos:
# a 3072px q90 source typically lands under the target by the 2000px/q85
# step. ImageOps.fit center-crops to the box, which matches how social
# clients crop anyway and guarantees the dimension bound regardless of
# aspect ratio (thumbnail() would only bound the long edge).
_REENCODE_PRESETS: list[tuple[int, int]] = [
    (3072, 90),
    (2048, 85),
    (1600, 82),
    (1280, 80),
    (1024, 78),
]

# Formats safe to re-encode losslessly at first (PNG/WebP recompress below
# target without quality loss only sometimes; JPEG sources dominate feed
# imagery, so format-preserving tries are best-effort only).
JPEG_TYPES = {"image/jpeg"}
PILLOW_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ImageToolUnavailable(Exception):
    """Pillow is not importable — callers fall back to skip behavior."""


def needs_downscale(img_bytes: bytes, max_bytes: int) -> bool:
    return len(img_bytes) > max_bytes


def downscale_image(
    img_bytes: bytes,
    content_type: str,
    max_bytes: int = DOWNSCALE_TARGET_BYTES,
) -> tuple[bytes, str] | None:
    """Return (bytes, content_type) fitting max_bytes, or None if impossible.

    Tries, in order:
      1. format-preserving recompress with quality steps (JPEG only)
      2. progressively smaller ImageOps.fit + re-encode to JPEG

    Returns None when Pillow is unavailable, the format is unsupported, or
    even the smallest preset exceeds max_bytes. Callers must keep their
    existing skip-on-None path — this helper never raises for bad input.
    """
    if len(img_bytes) <= max_bytes:
        return img_bytes, content_type
    if content_type not in PILLOW_TYPES:
        return None
    try:
        from PIL import Image, ImageOps
    except Exception:
        logger.warning("Pillow unavailable; oversized image cannot be re-encoded")
        return None

    import io

    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            im.load()
            # EXIF orientation must be baked in before crop/resize or rotated
            # photos come out sideways.
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")

            # Step 1: same-format quality reduction (JPEG only).
            if content_type in JPEG_TYPES:
                for quality in (90, 85, 80, 75):
                    buf = io.BytesIO()
                    im.save(buf, format="JPEG", quality=quality, optimize=True)
                    if buf.tell() <= max_bytes:
                        return buf.getvalue(), "image/jpeg"

            # Step 2: progressive fit + re-encode to JPEG.
            for box_size, quality in _REENCODE_PRESETS:
                fitted = ImageOps.fit(im, (box_size, box_size))
                buf = io.BytesIO()
                fitted.save(buf, format="JPEG", quality=quality, optimize=True)
                if buf.tell() <= max_bytes:
                    return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.warning("Image re-encode failed; falling back to skip", exc_info=True)
        return None

    logger.info(
        "Image could not be re-encoded under %d bytes even at smallest preset",
        max_bytes,
    )
    return None
