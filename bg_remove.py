#!/usr/bin/env python3
"""
bg_remove.py — image background remover (core engine + command-line tool).

The heavy lifting (finding the subject) is done by a U^2-Net / ISNet segmentation
model through the `rembg` package. Everything else — edge feathering, background
replacement, auto-crop, resizing, batch handling — is done here with Pillow.

    pip install "rembg[cpu]" pillow

Examples
--------
    # transparent PNG (default)
    python bg_remove.py photo.jpg

    # white background, save as JPEG
    python bg_remove.py photo.jpg -o photo_white.jpg --bg white

    # replace background with another photo, trim to the subject
    python bg_remove.py photo.jpg --bg-image studio.jpg --crop --feather 2

    # a whole folder, output into ./cutouts
    python bg_remove.py photos/ -o cutouts/ --bg "#0f172a"

Models (downloaded once, then cached locally):
    u2netp           4.5 MB   fastest, good for people/products      (default)
    u2net            176 MB   sharper edges, slower
    isnet-general-use 170 MB  best all-rounder for tricky subjects
    silueta          43 MB    tiny, decent on people
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageFilter

# Pillow registers some codecs (WebP in particular) only when the full plugin
# scan runs. Force it once here so saving "WEBP" never fails with KeyError.
try:
    Image.init()
except Exception:  # pragma: no cover - never fatal
    pass

# Optional: HEIC/HEIF photos (iPhone) open transparently when pillow-heif is present.
try:  # pragma: no cover - depends on the environment
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

MODELS = {
    "u2netp": "Fast, 4.5 MB. Good for people and products.",
    "u2net": "Sharper edges, 176 MB. The classic.",
    "isnet-general-use": "Best all-rounder for tricky subjects, 170 MB.",
    "silueta": "Tiny (43 MB), decent on people.",
}

DEFAULT_MODEL = "u2netp"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic"}

_session_cache: dict[str, object] = {}


def get_session(model: str = DEFAULT_MODEL):
    """Load (and cache) a rembg segmentation session."""
    if model in _session_cache:
        return _session_cache[model]
    try:
        from rembg import new_session
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "The 'rembg' package is required. Install it with:\n"
            '    pip install "rembg[cpu]" pillow\n'
            f"(original error: {exc})"
        ) from exc
    _session_cache[model] = new_session(model)
    return _session_cache[model]


def cutout_mask(img: Image.Image, model: str = DEFAULT_MODEL) -> Image.Image:
    """Return the soft alpha matte for `img` as an 'L' image, same size as input."""
    from rembg import remove

    rgb = img.convert("RGB")
    # rembg returns an RGBA image; its alpha channel *is* the matte.
    result = remove(rgb, session=get_session(model), post_process_mask=True)
    if result.size != rgb.size:
        result = result.resize(rgb.size, Image.LANCZOS)
    return result.getchannel("A")


def refine_mask(mask: Image.Image, feather: float = 0.0, grow: int = 0) -> Image.Image:
    """Grow/shrink the matte then soften it. `feather` is a blur radius in px."""
    if grow:
        mask = mask.filter(ImageFilter.MaxFilter(grow * 2 + 1)) if grow > 0 else mask.filter(
            ImageFilter.MinFilter(-grow * 2 + 1)
        )
    if feather and feather > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=feather))
    return mask


def _parse_color(value: str) -> tuple[int, int, int]:
    value = value.strip()
    named = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (128, 128, 128),
        "grey": (128, 128, 128),
        "red": (220, 38, 38),
        "blue": (37, 99, 235),
        "green": (22, 163, 74),
    }
    if value.lower() in named:
        return named[value.lower()]
    if value.startswith("#"):
        h = value.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(f"Bad hex colour: {value}")
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    parts = value.replace(";", ",").split(",")
    if len(parts) == 3:
        return tuple(max(0, min(255, int(float(p)))) for p in parts)  # type: ignore[return-value]
    raise ValueError(f"Unrecognised colour: {value!r}")


def _cover_resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale `img` to cover `size` and centre-crop it."""
    tw, th = size
    scale = max(tw / img.width, th / img.height)
    nw, nh = max(tw, int(round(img.width * scale))), max(th, int(round(img.height * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def remove_background(
    img: Image.Image,
    *,
    model: str = DEFAULT_MODEL,
    background: str = "transparent",
    bg_color: str = "white",
    bg_image: Image.Image | None = None,
    blur: float = 24.0,
    feather: float = 1.0,
    grow: int = 0,
    crop: bool = False,
    crop_padding: int = 0.04,
    max_size: int | None = None,
) -> Image.Image:
    """Remove the background from `img` and composite it onto a new one.

    background: 'transparent' | 'color' | 'image' | 'blur'
    Returns RGBA for transparent output, RGB otherwise.
    """
    original = img
    if max_size and max(original.size) > max_size:
        original = original.copy()
        original.thumbnail((max_size, max_size), Image.LANCZOS)

    original = original.convert("RGB")
    mask = refine_mask(cutout_mask(original, model=model), feather=feather, grow=grow)

    subject = original.convert("RGBA")
    subject.putalpha(mask)

    if crop:
        bbox = mask.point(lambda v: 255 if v > 12 else 0).getbbox()
        if bbox:
            pad = int(round(max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * crop_padding))
            box = (
                max(0, bbox[0] - pad),
                max(0, bbox[1] - pad),
                min(subject.width, bbox[2] + pad),
                min(subject.height, bbox[3] + pad),
            )
            subject = subject.crop(box)

    bg = background.lower()
    if bg == "transparent":
        return subject
    if bg == "color":
        canvas = Image.new("RGB", subject.size, _parse_color(bg_color))
    elif bg == "image":
        if bg_image is None:
            raise ValueError("background='image' needs bg_image")
        canvas = _cover_resize(bg_image.convert("RGB"), subject.size)
    elif bg == "blur":
        canvas = original.filter(ImageFilter.GaussianBlur(radius=blur))
        if canvas.size != subject.size:
            canvas = _cover_resize(canvas, subject.size)
    else:
        raise ValueError(f"Unknown background: {background!r}")

    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(subject)
    return canvas.convert("RGB")


def process_file(src: Path, dst: Path, *, fmt: str | None = None, quality: int = 95, **kwargs) -> Path:
    """Remove the background from a file and write the result. Returns the output path."""
    with Image.open(src) as img:
        img.load()
        out = remove_background(img, **kwargs)

    ext = (fmt or dst.suffix.lstrip(".") or "png").lower()
    dst.parent.mkdir(parents=True, exist_ok=True)

    if ext in {"jpg", "jpeg"}:
        if out.mode == "RGBA":
            # JPEG cannot store transparency: fall back to white.
            flat = Image.new("RGB", out.size, (255, 255, 255))
            flat.paste(out, (0, 0), out)
            out = flat
        out.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
    elif ext == "webp":
        try:
            out.save(dst, "WEBP", quality=quality, method=4)
        except (KeyError, OSError):
            # WebP support missing in this Pillow build: keep the result usable.
            dst = dst.with_suffix(".png")
            out.save(dst, "PNG", optimize=True)
    else:
        out.save(dst, "PNG", optimize=True)
    return dst


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bg_remove",
        description="Remove the background from images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Models:\n" + "\n".join(f"  {k:<20} {v}" for k, v in MODELS.items()),
    )
    p.add_argument("input", type=Path, help="image file or directory of images")
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="output file, or directory for batch mode (default: alongside input, *_nobg.png)")
    p.add_argument("-m", "--model", default=DEFAULT_MODEL, choices=sorted(MODELS),
                   help=f"segmentation model (default: {DEFAULT_MODEL})")
    p.add_argument("--bg", default="transparent", choices=["transparent", "color", "image", "blur"],
                   help="what to put behind the subject (default: transparent)")
    p.add_argument("--bg-color", default="white", help="colour for --bg color: name, #rrggbb or r,g,b")
    p.add_argument("--bg-image", type=Path, default=None, help="background image for --bg image")
    p.add_argument("--blur", type=float, default=24.0, help="blur radius for --bg blur (default: 24)")
    p.add_argument("--feather", type=float, default=1.0, help="edge softening in px (default: 1.0, 0 = hard edge)")
    p.add_argument("--grow", type=int, default=0, help="grow (+) or shrink (-) the subject edge in px")
    p.add_argument("--crop", action="store_true", help="trim the canvas to the subject")
    p.add_argument("--crop-padding", type=float, default=0.04, help="padding around a cropped subject (fraction)")
    p.add_argument("--max-size", type=int, default=None, help="downscale so the longest side is at most N px")
    p.add_argument("--format", default=None, choices=["png", "jpg", "webp"], help="output format override")
    p.add_argument("--quality", type=int, default=95, help="JPEG/WebP quality (default: 95)")
    p.add_argument("--suffix", default="_nobg", help="suffix for auto-named outputs (default: _nobg)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    src: Path = args.input
    if not src.exists():
        print(f"error: no such file or directory: {src}", file=sys.stderr)
        return 2

    bg_image = None
    if args.bg == "image":
        if not args.bg_image:
            print("error: --bg image requires --bg-image PATH", file=sys.stderr)
            return 2
        bg_image = Image.open(args.bg_image)
        bg_image.load()

    kwargs = dict(
        model=args.model,
        background=args.bg,
        bg_color=args.bg_color,
        bg_image=bg_image,
        blur=args.blur,
        feather=args.feather,
        grow=args.grow,
        crop=args.crop,
        crop_padding=args.crop_padding,
        max_size=args.max_size,
    )

    files: list[Path]
    if src.is_dir():
        files = sorted(f for f in src.iterdir() if f.suffix.lower() in IMAGE_EXTS)
        if not files:
            print(f"error: no images found in {src}", file=sys.stderr)
            return 2
        out_dir = args.output or (src / "nobg")
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        files = [src]
        out_dir = args.output if (args.output and args.output.is_dir()) else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

    ext = args.format or "png"
    failures = 0

    for i, f in enumerate(files, 1):
        if src.is_dir():
            dst = out_dir / f"{f.stem}{args.suffix}.{ext}"
        elif args.output and args.output.is_dir():
            dst = out_dir / f"{f.stem}{args.suffix}.{ext}"
        elif args.output:
            dst = args.output
        else:
            dst = f.with_name(f"{f.stem}{args.suffix}.{ext}")

        label = f"[{i}/{len(files)}] {f.name}"
        try:
            written = process_file(f, dst, fmt=args.format, quality=args.quality, **kwargs)
            print(f"{label} -> {written}")
        except Exception as exc:  # keep batch runs going
            failures += 1
            print(f"{label} failed: {exc}", file=sys.stderr)

    if failures:
        print(f"{failures} of {len(files)} file(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
