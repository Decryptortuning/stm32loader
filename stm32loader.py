#!/usr/bin/env python3

# -*- coding: utf-8 -*-
# vim: sw=4:ts=4:si:et:enc=utf-8

# Author: Ivan A-R <ivan@tuxotronic.org>
# Project page: http://tuxotronic.org/wiki/projects/stm32loader
#
# This file is part of stm32loader.
#
# stm32loader is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free
# Software Foundation; either version 3, or (at your option) any later
# version.
#
# stm32loader is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License
# along with stm32loader; see the file COPYING3.  If not see
# <http://www.gnu.org/licenses/>.

import sys, getopt
import serial
import time
import os
import struct
import tempfile

try:
    from progressbar import *
    usepbar = 1
except:
    usepbar = 0

# Verbose level
QUIET = 20

# these come from AN2606
chip_ids = {
    0x412: "STM32 Low-density",
    0x410: "STM32 Medium-density",
    0x414: "STM32 High-density",
    0x420: "STM32 Medium-density value line",
    0x428: "STM32 High-density value line",
    0x430: "STM32 XL-density",
    0x416: "STM32 Medium-density ultralow power line",
    0x411: "STM32F2xx",
    0x413: "STM32F4xx",
}

def mdebug(level, message):
    if(QUIET >= level):
        print(message, file=sys.stderr)


class CmdException(Exception):
    pass

class CommandInterface:
    extended_erase = 0

    def open(self, aport='/dev/tty.usbserial-ftCYPMYJ', abaudrate=115200) :
        self.sp = serial.Serial(
            port=aport,
            baudrate=abaudrate,     # baudrate
            bytesize=8,             # number of databits
            parity=serial.PARITY_EVEN,
            stopbits=1,
            xonxoff=0,              # don't enable software flow control
            rtscts=0,               # don't enable RTS/CTS flow control
            timeout=5,
            write_timeout=5,
            exclusive=True         # refuse a second cooperating serial owner
        )

    def _read_exact(self, length):
        data = self.sp.read(length)
        if len(data) != length:
            raise CmdException("Short read: expected %d bytes, received %d" % (length, len(data)))
        return data

    def _read_byte(self):
        b = self.sp.read(1)
        if not b:
            raise CmdException("Can't read port or timeout")
        return b[0]

    def _write_byte(self, b):
        self.sp.write(bytes([b & 0xFF]))

    def _wait_for_ask(self, info = ""):
        # wait for ask
        ask = self._read_byte()
        if ask == 0x79:
            # ACK
            return 1
        if ask == 0x1F:
            # NACK
            raise CmdException("NACK " + info)
        # Unknown response
        raise CmdException("Unknown response. " + info + ": " + hex(ask))

    def setPin(self, pin, value):
        try:
            exp_fd = open("/sys/class/gpio/export", "w")
            exp_fd.write(str(pin))
            exp_fd.close()
        except IOError:
            pass
            #print "Pin",pin," already exported"

        dir_fd = open("/sys/class/gpio/gpio"+str(pin)+"/direction","w")
        dir_fd.write("out")
        dir_fd.close()

        val_fd = open("/sys/class/gpio/gpio"+str(pin)+"/value","w")
        val_fd.write(str(value))
        val_fd.close()

    def reset(self):
        self.setPin(46, 1)
        time.sleep(0.1)
        self.setPin(46, 0)
        time.sleep(0.5)

    def initChip(self):
        # Set boot
        self.setPin(45, 1)
        self.reset()
        try:
            self.sp.reset_input_buffer()
        except AttributeError:
            self.sp.flushInput()
        try:
            self.sp.reset_output_buffer()
        except AttributeError:
            self.sp.flushOutput()

        self.sp.write(b"\x7F")       # Syncro

        # imperfect error simulation
        err_file = "/tmp/stm32_fail_init"
        if os.path.isfile(err_file):
            os.remove(err_file)
            mdebug(0, "simulating error")
            raise CmdException("simulation error")

        return self._wait_for_ask("Syncro")

    def releaseChip(self):
        self.setPin(45,0)
        self.reset()

    def cmdGeneric(self, cmd):
        self._write_byte(cmd)
        self._write_byte(cmd ^ 0xFF) # Control byte
        return self._wait_for_ask(hex(cmd))

    def cmdGet(self):
        if self.cmdGeneric(0x00):
            mdebug(10, "*** Get command");
            length = self._read_byte()
            version = self._read_byte()
            mdebug(10, "    Bootloader version: "+hex(version))
            dat = [hex(b) for b in self._read_exact(length)]
            if '0x44' in dat:
                self.extended_erase = 1
            mdebug(10, "    Available commands: "+", ".join(dat))
            self._wait_for_ask("0x00 end")
            return version
        else:
            raise CmdException("Get (0x00) failed")

    def cmdGetVersion(self):
        if self.cmdGeneric(0x01):
            mdebug(10, "*** GetVersion command")
            version = self._read_byte()
            self._read_exact(2)
            self._wait_for_ask("0x01 end")
            mdebug(10, "    Bootloader version: "+hex(version))
            return version
        else:
            raise CmdException("GetVersion (0x01) failed")

    def cmdGetID(self):
        if self.cmdGeneric(0x02):
            mdebug(10, "*** GetID command")
            length = self._read_byte()
            chip_id_bytes = self._read_exact(length + 1)
            self._wait_for_ask("0x02 end")
            return int.from_bytes(chip_id_bytes, "big")
        else:
            raise CmdException("GetID (0x02) failed")


    def _encode_addr(self, addr):
        byte3 = (addr >> 0) & 0xFF
        byte2 = (addr >> 8) & 0xFF
        byte1 = (addr >> 16) & 0xFF
        byte0 = (addr >> 24) & 0xFF
        crc = byte0 ^ byte1 ^ byte2 ^ byte3
        return bytes([byte0, byte1, byte2, byte3, crc])


    def cmdReadMemory(self, addr, lng):
        assert(lng <= 256)
        if self.cmdGeneric(0x11):
            mdebug(10, "*** ReadMemory command")
            self.sp.write(self._encode_addr(addr))
            self._wait_for_ask("0x11 address failed")
            N = (lng - 1) & 0xFF
            crc = N ^ 0xFF
            self.sp.write(bytes([N, crc]))
            self._wait_for_ask("0x11 length failed")
            data = self.sp.read(lng)
            if len(data) != lng:
                raise CmdException("Short read: expected %d bytes, received %d" % (lng, len(data)))
            return list(data)
        else:
            raise CmdException("ReadMemory (0x11) failed")


    def cmdGo(self, addr):
        if self.cmdGeneric(0x21):
            mdebug(10, "*** Go command")
            self.sp.write(self._encode_addr(addr))
            self._wait_for_ask("0x21 go failed")
        else:
            raise CmdException("Go (0x21) failed")


    def cmdWriteMemory(self, addr, data):
        assert(len(data) <= 256)
        if self.cmdGeneric(0x31):
            mdebug(10, "*** Write memory command")
            self.sp.write(self._encode_addr(addr))
            self._wait_for_ask("0x31 address failed")
            #map(lambda c: hex(ord(c)), data)
            lng = (len(data)-1) & 0xFF
            mdebug(10, "    %s bytes to write" % [lng+1]);
            self._write_byte(lng) # len really
            crc = 0xFF
            for c in data:
                crc = crc ^ c
            self.sp.write(bytes(data))
            self._write_byte(crc)
            self._wait_for_ask("0x31 programming failed")
            mdebug(10, "    Write memory done")
        else:
            raise CmdException("Write memory (0x31) failed")


    def cmdEraseMemory(self, sectors = None):
        # imperfect error simulation
        err_file = "/tmp/stm32_fail_erase"
        if os.path.isfile(err_file):
            os.remove(err_file)
            mdebug(0, "simulating error")
            raise CmdException("simulation error")

        if sectors is not None:
            sectors = list(sectors)
            limit = 0xFFF0 if self.extended_erase else 255
            max_page = 0xFFFF if self.extended_erase else 255
            if not sectors or len(sectors) > limit or any(type(x) is not int or not 0 <= x <= max_page for x in sectors):
                raise ValueError("Invalid erase page list")
        if self.extended_erase:
            return self.cmdExtendedEraseMemory(sectors)

        if self.cmdGeneric(0x43):
            mdebug(10, "*** Erase memory command")
            if sectors is None:
                # Global erase
                self.sp.write(bytes([0xFF, 0x00]))
            else:
                # Sectors erase
                crc = 0x0

                sec_len = (len(sectors)-1) & 0xff
                crc ^= sec_len
                self._write_byte(sec_len)

                for c in sectors:
                    crc ^= c
                    self._write_byte(c)
                self._write_byte(crc)
            self._wait_for_ask("0x43 erasing failed")
            mdebug(10, "    Erase memory done")
        else:
            raise CmdException("Erase memory (0x43) failed")

    def cmdExtendedEraseMemory(self, sectors=None):
        # AN3155: 16-bit MSB-first count-minus-one and page codes, then XOR.
        if sectors is None:
            payload = b"\xff\xff"
        else:
            sectors = list(sectors)
            if not sectors or len(sectors) > 0xFFF0 or any(type(x) is not int or not 0 <= x <= 0xFFFF for x in sectors):
                raise ValueError("Invalid extended erase page list")
            payload = (len(sectors) - 1).to_bytes(2, "big")
            payload += b"".join(x.to_bytes(2, "big") for x in sectors)
        checksum = 0
        for value in payload:
            checksum ^= value
        self.cmdGeneric(0x44)
        previous_timeout = self.sp.timeout
        try:
            self.sp.timeout = 30
            self.sp.write(payload + bytes([checksum]))
            self._wait_for_ask("0x44 erasing failed")
        finally:
            self.sp.timeout = previous_timeout

    def cmdWriteProtect(self, sectors):
        if self.cmdGeneric(0x63):
            mdebug(10, "*** Write protect command")
            self._write_byte((len(sectors)-1) & 0xFF)
            crc = 0xFF
            for c in sectors:
                crc = crc ^ c
                self._write_byte(c)
            self._write_byte(crc)
            self._wait_for_ask("0x63 write protect failed")
            mdebug(10, "    Write protect done")
        else:
            raise CmdException("Write Protect memory (0x63) failed")

    def cmdWriteUnprotect(self):
        if self.cmdGeneric(0x73):
            mdebug(10, "*** Write Unprotect command")
            self._wait_for_ask("0x73 write unprotect failed")
            self._wait_for_ask("0x73 write unprotect 2 failed")
            mdebug(10, "    Write Unprotect done")
        else:
            raise CmdException("Write Unprotect (0x73) failed")

    def cmdReadoutProtect(self):
        if self.cmdGeneric(0x82):
            mdebug(10, "*** Readout protect command")
            self._wait_for_ask("0x82 readout protect failed")
            self._wait_for_ask("0x82 readout protect 2 failed")
            mdebug(10, "    Read protect done")
        else:
            raise CmdException("Readout protect (0x82) failed")

    def cmdReadoutUnprotect(self):
        if self.cmdGeneric(0x92):
            mdebug(10, "*** Readout Unprotect command")
            # first (N)ACK is already read as part of cmdGeneric()
            self._wait_for_ask("0x92 readout unprotect 2 failed")
            mdebug(10, "    Read Unprotect done")
            # there does not appear to be a way to be notified when the
            # mass erase is complete... boo. hopefully this will be rare, i guess.
            time.sleep(20)

            # following readout unprotect, the system resets, so we must init once again.
            self.initChip()

        else:
            raise CmdException("Readout unprotect (0x92) failed")


# Complex commands section

    def readMemory(self, addr, lng):
        data = []
        if usepbar:
            widgets = ['Reading: ', Percentage(),', ', ETA(), ' ', Bar()]
            pbar = ProgressBar(widgets=widgets,maxval=lng, term_width=79).start()

        while lng > 256:
            if usepbar:
                pbar.update(pbar.maxval-lng)
            else:
                mdebug(5, "Read %(len)d bytes at 0x%(addr)X" % {'addr': addr, 'len': 256})
            data = data + self.cmdReadMemory(addr, 256)
            addr = addr + 256
            lng = lng - 256
        if usepbar:
            pbar.update(pbar.maxval-lng)
            pbar.finish()
        else:
            mdebug(5, "Read %(len)d bytes at 0x%(addr)X" % {'addr': addr, 'len': 256})
        data = data + self.cmdReadMemory(addr, lng)
        return data

    def writeMemory(self, addr, data):
        lng = len(data)
        if usepbar:
            widgets = ['Writing: ', Percentage(),' ', ETA(), ' ', Bar()]
            pbar = ProgressBar(widgets=widgets, maxval=lng, term_width=79).start()

        offs = 0
        while lng > 256:
            if usepbar:
                pbar.update(pbar.maxval-lng)
            else:
                mdebug(5, "Write %(len)d bytes at 0x%(addr)X" % {'addr': addr, 'len': 256})
            self.cmdWriteMemory(addr, data[offs:offs+256])
            offs = offs + 256
            addr = addr + 256
            lng = lng - 256
        if usepbar:
            pbar.update(pbar.maxval-lng)
            pbar.finish()
        else:
            mdebug(5, "Write %(len)d bytes at 0x%(addr)X" % {'addr': addr, 'len': 256})
        self.cmdWriteMemory(addr, data[offs:offs+lng] + ([0xFF] * (256-lng)) )




    def __init__(self) :
        pass


def usage():
    print("""Usage: %s [-hqVewvru] [-l length] [-p port] [-b baud] [-a addr] [-g addr] [file.bin]
    -h          This help
    -q          Quiet
    -V          Verbose
    -e          Erase
    -w          Write
    -v          Verify
    -r          Read
    -l length   Length of read
    -p port     Serial port (default: /dev/tty.usbserial-ftCYPMYJ)
    -b baud     Baud speed (default: 115200)
    -a addr     Target address
    -g addr     Address to start running at (0x08000000, usually)
    -u          Unprotect chip if erase fails

    ./stm32loader.py -e -w -v example/main.bin

    """ % sys.argv[0])


ARTOO_LAYOUT = b'OS5JNL1!' + struct.pack('<IHH', 0x0803f000, 172, 1)


def validate_artoo_image(data):
    data = bytes(data)
    if not 320 <= len(data) <= 0x3f000 or data[304:320] != ARTOO_LAYOUT:
        raise ValueError("Artoo requires a journal-v1 image below page126; legacy downgrade forbidden")
    sp, pc = struct.unpack_from('<II', data)
    if not (0x20000000 < sp <= 0x2000c000 and sp % 8 == 0 and
            pc & 1 and 0x08000000 <= (pc & ~1) < 0x08000000 + len(data)):
        raise ValueError("Invalid Artoo image vectors")


def art_param_record(page):
    if len(page) != 2048 or page[:4] != b'OSJ1' or page[4:8] != b'\x01\x00\xac\x00' or page[188:192] != b'\x5a\xa5\xff\xff':
        return None
    crc = 0xffffffff
    for value in page[:12] + page[16:188]:
        crc ^= value
        for _ in range(8): crc = (crc >> 1) ^ (0xedb88320 if crc & 1 else 0)
    if struct.unpack_from('<I',page,12)[0] != (crc ^ 0xffffffff): return None
    return struct.unpack_from('<I',page,8)[0], page[16:188]


def classify_artoo_pages(saved):
    if len(saved) != 4096: raise CmdException("Short parameter pages")
    a,b = saved[:2048],saved[2048:]
    ar,br = art_param_record(a),art_param_record(b)
    if ar and br:
        delta=(ar[0]-br[0]) & 0xffffffff
        if delta==0x80000000 or (delta==0 and ar[1]!=br[1]):
            raise CmdException("Ambiguous parameter generations")
        return ar[1] if delta<0x80000000 else br[1]
    if ar or br: return (ar or br)[1]
    if b[:4]==b'OSJ1' or b[172:] != b'\xff'*(2048-172):
        raise CmdException("Corrupt/unknown journal; preserve for explicit recovery")
    return b[:172]


def preserve_artoo_pages(cmd, chip, directory):
    if chip != 0x414 or bytes(cmd.readMemory(0x1ffff7e0, 2)) != b'\x00\x01':
        raise CmdException("Artoo requires verified high-density256KiB geometry")
    # USER option-byte bit0 selects software IWDG; never change option bytes.
    options = bytes(cmd.readMemory(0x1ffff800, 4))
    if len(options) != 4 or not options[2] & 1:
        raise CmdException("Hardware-start watchdog mode is not qualified for ROM updates")
    saved = bytes(cmd.readMemory(0x0803f000, 4096))
    if len(saved) != 4096:
        raise CmdException("Short parameter journal read")
    classify_artoo_pages(saved)
    fd, path = tempfile.mkstemp(prefix='artoo-pages-', suffix='.bin', dir=directory)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(saved); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)
    print("Preserved parameter pages: " + path)
    return saved


def main(argv=None):
    global QUIET
    if argv is None:
        argv = sys.argv[1:]


    # Import Psyco if available
    try:
        import psyco
        psyco.full()
        print("Using Psyco...")
    except ImportError:
        pass

    conf = {
            'port': '/dev/tty.usbserial-ftCYPMYJ',
            'baud': 115200,
            'address': 0x08000000,
            'erase': 0,
            'write': 0,
            'verify': 0,
            'read': 0,
            'go_addr':-1,
            'unprotect': 0,
        }

# http://www.python.org/doc/2.5.2/lib/module-getopt.html

    try:
        opts, args = getopt.getopt(argv, "hqVewuvrs:p:b:a:l:g:", ["artoo", "artoo-backup-dir="])
    except getopt.GetoptError as err:
        # print help information and exit:
        print(str(err)) # will print something like "option -a not recognized"
        usage()
        sys.exit(2)

    QUIET = 5

    for o, a in opts:
        if o == '--artoo':
            conf['artoo'] = True
        elif o == '--artoo-backup-dir':
            conf['artoo-backup-dir'] = a
        elif o == '-V':
            QUIET = 10
        elif o == '-q':
            QUIET = 0
        elif o == '-h':
            usage()
            sys.exit(0)
        elif o == '-e':
            conf['erase'] = 1
        elif o == '-s':
            conf['erase-sectors'] = a
        elif o == '-w':
            conf['write'] = 1
        elif o == '-v':
            conf['verify'] = 1
        elif o == '-r':
            conf['read'] = 1
        elif o == '-p':
            conf['port'] = a
        elif o == '-b':
            conf['baud'] = int(a, 0)
        elif o == '-a':
            conf['address'] = int(a, 0)
        elif o == '-g':
            conf['go_addr'] = int(a, 0)
        elif o == '-l':
            conf['len'] = int(a, 0)
        elif o == '-u':
            conf['unprotect'] = 1
        else:
            assert False, "unhandled option"

    # Validate selected files and lengths before entering the ROM bootloader.
    if conf['write'] or conf['verify'] or conf['read']:
        if len(args) != 1:
            raise ValueError("Exactly one firmware/input-output filename is required")
    if conf['write'] or conf['verify']:
        with open(args[0], 'rb') as stream:
            data = list(stream.read())
        if not data:
            raise ValueError("Firmware file must not be empty")
    if conf['read'] and (not conf.get('len') or conf['len'] <= 0):
        raise ValueError("Read length must be positive")
    if conf['baud'] <= 0:
        raise ValueError("Baud rate must be positive")
    if 'erase-sectors' in conf and int(conf['erase-sectors']) <= 0:
        raise ValueError("Erase sector count must be positive")

    if conf.get('artoo'):
        if (not conf['write'] or not conf['verify'] or conf['erase'] or conf['unprotect'] or
                conf['read'] or conf['address'] != 0x08000000 or conf['go_addr'] != -1 or
                int(conf.get('erase-sectors', 0)) != 126 or not conf.get('artoo-backup-dir')):
            raise ValueError("Artoo mode requires -wv -s126, backup directory, no mass erase/unprotect/Go")
        validate_artoo_image(data)
    elif (conf['write'] or conf['verify']) and bytes(data[304:320]) == ARTOO_LAYOUT:
        raise ValueError("Journal firmware requires --artoo guarded programming")
    mutation_started = False
    art_success = False
    cmd = CommandInterface()
    cmd.open(conf['port'], conf['baud'])
    mdebug(10, "Open port %(port)s, baud %(baud)d" % {'port':conf['port'], 'baud':conf['baud']})
    try:
        try:
            cmd.initChip()
        except:
            mdebug(0, "Can't init. Ensure that BOOT0 is enabled and reset device")
            sys.exit(1)

        bootversion = cmd.cmdGet()
        mdebug(0, "Bootloader version %X" % bootversion)
        id = cmd.cmdGetID()
        mdebug(0, "Chip id: 0x%x (%s)" % (id, chip_ids.get(id, "Unknown")))
#    cmd.cmdGetVersion()
#    cmd.cmdGetID()
#    cmd.cmdReadoutUnprotect()
#    cmd.cmdWriteUnprotect()
#    cmd.cmdWriteProtect([0, 1])

        saved_pages = None
        if conf.get('artoo'):
            saved_pages = preserve_artoo_pages(cmd, id, conf['artoo-backup-dir'])
            mutation_started = True
        try:
            if conf['erase']:
                cmd.cmdEraseMemory()
            elif 'erase-sectors' in conf:
                # cmdEraseMemory() accepts a list of sectors,
                # we treat the arg as a sector count, at offset 0
                sectors = range(int(conf['erase-sectors']))
                cmd.cmdEraseMemory(sectors)

        except CmdException:
            # Erase can fail due to readout protection.
            # this was observed once as a process problem in production,
            # there may be other reasons for this failure in the future.
            if conf['unprotect']:
                mdebug(0, "EraseMemory failed, disabling readout protection")
                cmd.cmdReadoutUnprotect()
            else:
                # can't continue on
                mdebug(0, "EraseMemory failed, quitting")
                if not conf.get("artoo"): cmd.releaseChip()
                raise CmdException("Erase failed; no automatic retry")

        if conf['write']:
            cmd.writeMemory(conf['address'], data)

        if conf['verify']:
            verify = cmd.readMemory(conf['address'], len(data))
            if(data == verify):
                print("Verification OK")
            else:
                if len(data) != len(verify):
                    raise CmdException("Firmware verification short read: expected %d bytes, received %d" % (len(data), len(verify)))
                print("Verification FAILED")
                print(str(len(data)) + ' vs ' + str(len(verify)))
                imax = len(data)
                if imax > 100:
                    imax = 100
                for i in range(0, imax):
                    if data[i] != verify[i]:
                        print(hex(i) + ': ' + hex(data[i]) + ' vs ' + hex(verify[i]))

                raise CmdException("Firmware verification failed")

        if conf.get('artoo'):
            if bytes(cmd.readMemory(0x0803f000, 4096)) != saved_pages:
                raise CmdException("Parameter pages changed; retaining ROM for recovery")
            art_success = True

        if not conf['write'] and conf['read']:
            rdata = cmd.readMemory(conf['address'], conf['len'])
            open(args[0], 'wb').write(bytes(rdata))

        if conf['go_addr'] != -1:
            cmd.cmdGo(conf['go_addr'])

    finally:
        try:
            if not conf.get("artoo") or not mutation_started or art_success:
                cmd.releaseChip()
        finally:
            cmd.sp.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
