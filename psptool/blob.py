import re
import struct

from typing import List

import utils
from firmware import Firmware
from directory import Directory


class Blob(utils.NestedBuffer):
    _FIRMWARE_ENTRY_MAGIC = b'\xAA\x55\xAA\x55'
    _FIRMWARE_ENTRY_TABLE_BASE_ADDRESS = 0x20000

    _FIRMWARE_ENTRY_TYPES = [  # typedef struct _FIRMWARE_ENTRY_TABLE {
        # 'signature', UINT32  Signature;    ///< Signature should be 0x55AA55AAul
        'IMC',       # UINT32  ImcRomBase;   ///< Base Address for Imc Firmware
        'GMC',       # UINT32  GecRomBase;   ///< Base Address for Gmc Firmware
        'XHCI',      # UINT32  XHCRomBase;   ///< Base Address for XHCI Firmware
        'PSP_DIR',   # UINT32  PspDirBase;   ///< Base Address for PSP directory
        'PSP_NEW',   # UINT32  NewPspDirBase;///< Base Address of PSP directory from program start from ST
        'BHD',       # UINT32  BhdDirBase;   ///< Base Address for BHD directory
    ]

    def __init__(self, buffer: bytearray, size: int):
        super().__init__(buffer, size)

        self.directories: List[Directory] = []
        self.firmwares: List[Firmware] = []
        self.unique_entries = set()

        self._parse_agesa_version()

        self._find_entry_table()
        self._parse_entry_table()

        # todo: info members:
        #  self.range = (min, max)

    def __repr__(self):
        return f'Blob(agesa_version={self.agesa_version}, len(firmwares)={len(self.firmwares)}, ' \
               f'len(directories)={len(self.directories)})'

    def _parse_agesa_version(self):
        # from https://www.amd.com/system/files/TechDocs/44065_Arch2008.pdf

        # todo: use NestedBuffers instead of saving by value
        start = self.get_buffer().find(b'AGESA!')
        version_string = self[start:start + 36]

        agesa_magic = version_string[0:8]
        component_name = version_string[9:16]
        version = version_string[16:29]

        self.agesa_version = str(b''.join([agesa_magic, b' ', component_name, version]), 'ascii')

    def _find_entry_table(self):
        # AA55AA55 is to unspecific, so we require a word of padding before (to be tested)
        m = re.search(b'\xff\xff\xff\xff' + self._FIRMWARE_ENTRY_MAGIC, self.get_buffer())
        if m is None:
            utils.print_error_and_exit('Could not find any Firmware Entry Table!')
        fet_offset = m.start() + 4

        # Find out its size by determining an FF-word as termination
        fet_size = 0
        while fet_offset <= len(self.get_buffer()) - 4:
            if self[(fet_offset + fet_size):(fet_offset + fet_size + 4)] != b'\xff\xff\xff\xff':
                fet_size += 4
            else:
                break

        # Normally, the FET is found at offset 0x20000 in the ROM file
        # If the actual offset is bigger because of e.g. additional ROM headers, shift our NestedBuffer accordingly
        rom_offset = fet_offset - self._FIRMWARE_ENTRY_TABLE_BASE_ADDRESS
        if rom_offset != 0:
            utils.print_warning('Found Firmware Entry Table at 0x%x instead of 0x%x. All addresses will lack an offset '
                                'of 0x%x.' % (fet_offset, self._FIRMWARE_ENTRY_TABLE_BASE_ADDRESS, rom_offset))

        self.buffer_offset = rom_offset

        # Now the FET can be found at its usual static offset of 0x20000 in shifted NestedBuffer
        self.firmware_entry_table = utils.NestedBuffer(self, fet_size, self._FIRMWARE_ENTRY_TABLE_BASE_ADDRESS)

    def _parse_entry_table(self) -> (List[Firmware], List[Directory]):
        entries = utils.chunker(self.firmware_entry_table[4:], 4)

        for index, entry in enumerate(entries):
            firmware_type = self._FIRMWARE_ENTRY_TYPES[index] if index < len(self._FIRMWARE_ENTRY_TYPES) else 'unknown'
            address = struct.unpack('<I', entry)[0] & 0x00FFFFFF

            # assumption: offset == 0 is an invalid entry
            if address != 0:
                directory = self[address:address + 16 * 8]
                magic = directory[:4]

                # either this entry points to a PSP directory directly
                if magic in [b'$PSP', b'$BHD']:
                    directory = Directory(self, address, firmware_type)
                    self.directories.append(directory)

                    # if this Directory points to a secondary directory: add it, too
                    if directory.secondary_directory_address is not None:
                        secondary_directory = Directory(self, directory.secondary_directory_address, 'secondary')
                        self.directories.append(secondary_directory)

                # or this entry points to a combo-directory (i.e. two directories)
                elif magic == b'2PSP':
                    psp_dir_one_addr = struct.unpack('<I', directory[10*4:10*4+4])[0] & 0x00FFFFFF
                    psp_dir_two_addr = struct.unpack('<I', directory[14*4:14*4+4])[0] & 0x00FFFFFF

                    for address in [psp_dir_one_addr, psp_dir_two_addr]:
                        directory = Directory(self, address, firmware_type)
                        self.directories.append(directory)

                        # if this Directory points to a secondary directory: add it, too
                        if directory.secondary_directory_address is not None:
                            secondary_directory = Directory(self, directory.secondary_directory_address, 'secondary')
                            self.directories.append(secondary_directory)

                # or this entry is unparsable and thus a firmware
                else:
                    firmware = Firmware(self, address, firmware_type, magic)
                    self.firmwares.append(firmware)

    def get_entry_by_type(self, type_):
        for entry in self.unique_entries:
            if entry.type == type_:
                return entry
        return None
