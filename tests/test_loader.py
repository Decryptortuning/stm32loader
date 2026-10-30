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


class ArtooJournalTests(unittest.TestCase):
    def image(self):
        import struct
        return struct.pack('<II',0x2000c000,0x08000141)+b'\0'*296+loader.ARTOO_LAYOUT+b'\0'*64

    def command(self):
        command=Mock();command.cmdGet.return_value=0x22;command.cmdGetID.return_value=0x414
        image=self.image();pages=b'\xff'*4096
        def read(addr,size):
            return list({(0x1ffff7e0,2):b'\0\x01',(0x1ffff800,4):b'\xa5\x5a\xff\0',
                         (0x0803f000,4096):pages,(0x08000000,len(image)):image}[(addr,size)])
        command.readMemory.side_effect=read
        return command

    def test_guarded_update_preserves_two_pages_before_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'image.bin';image.write_bytes(self.image());command=self.command()
            with patch.object(loader,'CommandInterface',return_value=command):
                self.assertEqual(loader.main(['--artoo','--artoo-backup-dir',tmp,'-wv','-s','126',str(image)]),0)
            self.assertEqual(list(command.cmdEraseMemory.call_args.args[0]),list(range(126)))
            self.assertEqual(list(Path(tmp).glob('artoo-pages-*'))[0].read_bytes(),b'\xff'*4096)
            command.releaseChip.assert_called_once();command.cmdReadoutUnprotect.assert_not_called()

    def test_reject_incompatible_flags_and_legacy_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'image.bin';image.write_bytes(self.image())
            for flags in [['-wv','-s','127'],['-wvu','-s','126'],['-wve','-s','126'],['-w','-s','126']]:
                with patch.object(loader,'CommandInterface') as ctor,self.assertRaises(ValueError):
                    loader.main(['--artoo','--artoo-backup-dir',tmp,*flags,str(image)])
                ctor.assert_not_called()
            image.write_bytes(b'x'*384)
            with patch.object(loader,'CommandInterface') as ctor,self.assertRaises(ValueError):
                loader.main(['--artoo','--artoo-backup-dir',tmp,'-wv','-s','126',str(image)])
            ctor.assert_not_called()

    def test_programming_failure_stays_in_rom_and_closes_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'image.bin';image.write_bytes(self.image());command=self.command()
            command.writeMemory.side_effect=loader.CmdException('NACK')
            with patch.object(loader,'CommandInterface',return_value=command),self.assertRaises(loader.CmdException):
                loader.main(['--artoo','--artoo-backup-dir',tmp,'-wv','-s','126',str(image)])
            command.releaseChip.assert_not_called();command.sp.close.assert_called_once()

    def test_bad_geometry_or_unknown_storage_never_erases(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'image.bin';image.write_bytes(self.image())
            for address,bad in [(0x1ffff7e0,b'\0\x02'),(0x1ffff800,b'\xa5\x5a\0\xff'),(0x0803f000,b'x'*4096)]:
                command=self.command();good=command.readMemory.side_effect
                command.readMemory.side_effect=lambda a,n: list(bad) if a==address else good(a,n)
                with patch.object(loader,'CommandInterface',return_value=command),self.assertRaises(loader.CmdException):
                    loader.main(['--artoo','--artoo-backup-dir',tmp,'-wv','-s','126',str(image)])
                command.cmdEraseMemory.assert_not_called();command.writeMemory.assert_not_called()


class ExistingJournalGuardTests(unittest.TestCase):
    def test_generic_legacy_erase_unprotect_and_write_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            image=Path(tmp)/'old.bin';image.write_bytes(b'legacy')
            for installed, pages in [(loader.ARTOO_LAYOUT,b'\xff'*4096),
                                     (b'\xff'*16,b'OSJ1'+b'\xff'*4092),
                                     (b'\xff'*16,b'\xff'*2048+b'OSJ1'+b'\xff'*2044)]:
                for flags in [['-wv','-s','127',str(image)],['-e'],['-eu'],['-w',str(image)]]:
                    command=Mock();command.cmdGet.return_value=0x22;command.cmdGetID.return_value=0x414
                    command.readMemory.side_effect=lambda a,n:list({0x1ffff7e0:b'\0\x01',0x08000130:installed,0x0803f000:pages}[a])
                    with patch.object(loader,'CommandInterface',return_value=command),self.assertRaisesRegex(loader.CmdException,'requires --artoo'):
                        loader.main(flags)
                    command.cmdEraseMemory.assert_not_called();command.writeMemory.assert_not_called();command.cmdReadoutUnprotect.assert_not_called()
