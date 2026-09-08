"""Generate the multi-resolution Windows icon used by the app and installer."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "renmin-university-of-china-logo.png"
OUTPUT = PROJECT_ROOT / "packaging" / "app_icon.ico"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> None:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"Icon source not found: {SOURCE}")

    with Image.open(SOURCE) as source:
        image = source.convert("RGBA")
        if image.width != image.height:
            raise ValueError(
                f"Windows icon source must be square, got {image.width}x{image.height}"
            )
        image.save(OUTPUT, format="ICO", sizes=[(size, size) for size in ICON_SIZES])

    print(f"Generated Windows icon: {OUTPUT}")


if __name__ == "__main__":
    main()
