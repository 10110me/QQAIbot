import re
import unittest
from pathlib import Path


WEB = Path(__file__).parents[1] / "src/osa/interfaces/web"


class MobileUITests(unittest.TestCase):
    def test_sidebar_page_scrolling_and_dialog_at_phone_width(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidates = [
                Path(playwright.chromium.executable_path),
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            ]
            executable = next((path for path in candidates if path.is_file()), None)
            if executable is None:
                self.skipTest("Chromium is not installed")
            browser = playwright.chromium.launch(headless=True, executable_path=str(executable))
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1,
                                        reduced_motion="reduce")
                page.set_default_timeout(5000)
                html = (WEB / "index.html").read_text(encoding="utf-8")
                html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
                html = re.sub(r"<link\b[^>]*>", "", html, flags=re.I)
                page.set_content(html, wait_until="domcontentloaded")
                page.add_style_tag(content="body{margin:0}")
                page.add_style_tag(path=str(WEB / "redesign.css"))
                page.add_style_tag(content=".overflow-y-auto{overflow-y:auto;overscroll-behavior-y:contain}")
                page.evaluate("document.body.classList.remove('auth-locked'); document.getElementById('login-screen').hidden=true")
                self.assertFalse(page.locator("#mobile-nav-toggle").count())
                self.assertTrue(page.locator("body > aside").is_visible())
                self.assertEqual(page.locator("main").evaluate("element => getComputedStyle(element).overflowY"), "visible")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                page.evaluate("window.switchTab = () => {}")
                page.locator("#tab-models").click()
                self.assertTrue(page.locator("body > aside").is_visible())
                page.locator("#sec-overview").evaluate("element => element.insertAdjacentHTML('beforeend', '<div style=\"height:2000px\"></div>')")
                page.mouse.move(280, 600)
                page.mouse.wheel(0, 500)
                page.wait_for_timeout(100)
                self.assertGreater(page.evaluate("window.scrollY"), 0)

                page.locator("#modal").evaluate("element => element.showModal()")
                self.assertEqual(page.locator("#modal").evaluate("element => Math.round(element.getBoundingClientRect().width)"), 390)
                self.assertEqual(page.locator("#modal").evaluate("element => Math.round(element.getBoundingClientRect().height)"), 844)
                page.locator("#modal").evaluate("element => element.close()")
                page.set_viewport_size({"width": 320, "height": 640})
                overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
                    .filter(element => element.getBoundingClientRect().right > 321 && getComputedStyle(element).position !== 'fixed')
                    .slice(0, 12).map(element => [element.tagName, element.id, element.className, Math.round(element.getBoundingClientRect().right)])""")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 320, overflow)
                page.set_viewport_size({"width": 1024, "height": 768})
                self.assertTrue(page.locator("body > aside").is_visible())
            finally:
                browser.close()
