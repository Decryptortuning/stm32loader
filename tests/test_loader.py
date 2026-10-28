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
                command.sp.close.assert_called_once()
                command.cmdReadoutUnprotect.assert_not_called()

    def test_standard_page_erase_preserves_page_127(self):
        command = loader.CommandInterface()
        command.sp = Mock()
        with patch.object(command, 'cmdGeneric'), patch.object(command, '_wait_for_ask'):
            command.cmdEraseMemory(range(127))
        frame = b''.join(call.args[0] for call in command.sp.write.call_args_list)
        self.assertEqual(frame, bytes([126]) + bytes(range(127)) + b"\x01")

    def test_extended_page_erase_never_becomes_mass_erase(self):
        command = loader.CommandInterface()
        command.extended_erase = 1
        command.sp = Mock(timeout=5)
        with patch.object(command, 'cmdGeneric') as generic, patch.object(command, '_wait_for_ask'):
            command.cmdEraseMemory(range(127))
        generic.assert_called_once_with(0x44)
        frame = command.sp.write.call_args.args[0]
        self.assertEqual(frame[:2], b"\x00\x7e")
        pages = [int.from_bytes(frame[i:i+2], 'big') for i in range(2, len(frame)-1, 2)]
        self.assertEqual(pages, list(range(127)))
        self.assertNotIn(127, pages)
        self.assertEqual(len(frame), 257)
        from functools import reduce
        from operator import xor
        self.assertEqual(reduce(xor, frame), 0)
        self.assertEqual(command.sp.timeout, 5)

    def test_invalid_erase_pages_send_nothing(self):
        for extended in (0, 1):
            for pages in ([], [-1], [65536]):
                command = loader.CommandInterface()
                command.extended_erase = extended
                command.sp = Mock()
                with patch.object(command, 'cmdGeneric') as generic, self.assertRaises(ValueError):
                    command.cmdEraseMemory(pages)
                generic.assert_not_called()
                command.sp.write.assert_not_called()

    def test_extended_timeout_restored_on_nack(self):
        command = loader.CommandInterface()
        command.sp = Mock(timeout=5)
        with patch.object(command, 'cmdGeneric'), patch.object(command, '_wait_for_ask', side_effect=loader.CmdException('NACK')):
            with self.assertRaises(loader.CmdException):
                command.cmdExtendedEraseMemory([0])
        self.assertEqual(command.sp.timeout, 5)

    def test_cleanup_closes_uart_even_if_reset_fails(self):
        command = Mock()
        command.cmdGet.return_value = 0x22
        command.cmdGetID.return_value = 0x414
        command.releaseChip.side_effect = RuntimeError('reset failed')
        with patch.object(loader, 'CommandInterface', return_value=command), self.assertRaises(RuntimeError):
            loader.main([])
        command.sp.close.assert_called_once()

    def test_get_id_short_reply_fails(self):
        command = loader.CommandInterface()
        command.sp = Mock()
        command.sp.read.side_effect = [b"\x01", b"\x04"]
        with patch.object(command, 'cmdGeneric'), self.assertRaisesRegex(loader.CmdException, 'Short read'):
            command.cmdGetID()

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
