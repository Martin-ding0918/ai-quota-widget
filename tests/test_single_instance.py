import sys
import unittest
from uuid import uuid4

from widget_app.single_instance import SingleInstance


@unittest.skipUnless(sys.platform == "win32", "Windows named mutex")
class TestSingleInstance(unittest.TestCase):
    def test_second_instance_detected_then_released(self):
        name = f"Local\\AIQuotaWidget-test-{uuid4()}"
        first = SingleInstance(name)
        try:
            self.assertFalse(first.already_running)
            second = SingleInstance(name)
            try:
                self.assertTrue(second.already_running)
            finally:
                second.close()
            self.assertIsNone(second._handle)
        finally:
            first.close()
        third = SingleInstance(name)
        try:
            self.assertFalse(third.already_running)
        finally:
            third.close()
