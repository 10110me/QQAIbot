import tempfile
import unittest
from pathlib import Path
from workers.browser.start_browser import clean_display_lock


class BrowserStartupTests(unittest.TestCase):
    def test_removes_orphaned_display_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);proc=root/'proc';proc.mkdir();tmp=root/'tmp';tmp.mkdir()
            (tmp/'.X11-unix').mkdir();(tmp/'.X99-lock').write_text('8');(tmp/'.X11-unix'/'X99').touch()
            clean_display_lock(proc,tmp)
            self.assertFalse((tmp/'.X99-lock').exists())
            self.assertFalse((tmp/'.X11-unix'/'X99').exists())

    def test_never_removes_lock_of_running_display(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);proc=root/'proc';proc.mkdir();tmp=root/'tmp';tmp.mkdir()
            process=proc/'8';process.mkdir();(process/'cmdline').write_bytes(b'/usr/bin/Xvfb\0:99\0-screen\0')
            (tmp/'.X99-lock').write_text('8')
            clean_display_lock(proc,tmp)
            self.assertTrue((tmp/'.X99-lock').exists())
