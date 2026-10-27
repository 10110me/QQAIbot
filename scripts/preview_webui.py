"""Optional development preview; requires Playwright and an installed Chromium."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
        await page.set_content((root / "src/osa/interfaces/web/index.html").read_text())
        await page.screenshot(path=str(root / "docs/webui-preview.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.screenshot(path=str(root / "docs/webui-mobile.png"), full_page=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
