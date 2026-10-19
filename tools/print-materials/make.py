"""Rebuild the printed materials from config.py.

    pip install flask playwright pillow && python -m playwright install chromium
    python make.py

Writes docs/images/print/steps-page-1.png, steps-page-2.png and tarpaulin.jpg.
"""

import asyncio

import screens


async def main():
    await screens.capture()
    import steps_pages  # noqa: E402  (reads the screens just captured)
    import tarpaulin  # noqa: E402

    await steps_pages.main()
    await tarpaulin.main()


if __name__ == "__main__":
    asyncio.run(main())
