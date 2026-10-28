"""ROM loader behavior tests; no serial device is available to this module."""
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import sys

SERIAL = types.ModuleType('serial')
SERIAL.Serial = Mock(side_effect=AssertionError('Hardware access forbidden'))
with patch.dict(sys.modules, {'serial': SERIAL}):
    spec = importlib.util.spec_from_file_location('loader_under_test', Path(__file__).resolve().parents[1]/'stm32loader.py')
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)


class LoaderTests(unittest.TestCase):
    def test_import_is_hardware_free(self):
        SERIAL.Serial.assert_not_called()

    def test_short_rom_read_is_failure(self):
        command = loader.CommandInterface()
        command.sp = Mock()
        command.sp.read.return_value = b'\x01'
        with patch.object(command, 'cmdGeneric', return_value=True), patch.object(command, '_wait_for_ask'), \
                self.assertRaisesRegex(loader.CmdException, 'Short read'):
            command.cmdReadMemory(0x08000000, 2)

    def test_cli_verify_success_mismatch_shortread_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            firmware = Path(directory)/'firmware.bin'
            firmware.write_bytes(b'\x01\x02')
            for content, fails in [([1, 2], False), ([2, 1], True), ([1], True)]:
                command = Mock()
                command.cmdGet.return_value = 0x22
                command.cmdGetID.return_value = 0x414
                command.readMemory.return_value = content
                with self.subTest(content=content), patch.object(loader, 'CommandInterface', return_value=command):
                    if fails:
                        with self.assertRaises(loader.CmdException):
                            loader.main(['-v', str(firmware)])
                    else:
                        self.assertEqual(loader.main(['-v', str(firmware)]), 0)
                command.releaseChip.assert_called_once()
                command.cmdReadoutUnprotect.assert_not_called()

    def test_invalid_input_never_enters_bootloader(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory)/'empty.bin'
            empty.touch()
            for argv in [['-w'], ['-w', str(empty)], ['-r', '-l', '0', str(empty)],
                         ['-b', '__import__("os").system("false")'], ['-b', '-1']]:
                with self.subTest(argv=argv), patch.object(loader, 'CommandInterface') as command, self.assertRaises(ValueError):
                    loader.main(argv)
                command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
