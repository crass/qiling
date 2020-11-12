#!/usr/bin/env python3
# 
# Cross Platform and Multi Architecture Advanced Binary Emulation Framework
# Built on top of Unicorn emulator (www.unicorn-engine.org) 
import sys
import os
import string

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection
from elftools.elf.sections import SymbolTableSection
from elftools.elf.descriptions import describe_reloc_type

from qiling.const import *
from qiling.exception import *
from .loader import QlLoader
from qiling.os.linux.function_hook import FunctionHook
from qiling.os.linux.syscall_nums import *
from qiling.os.linux.kernel_api.hook import *

AT_NULL = 0
AT_IGNORE = 1
AT_EXECFD = 2
AT_PHDR = 3
AT_PHENT = 4
AT_PHNUM = 5
AT_PAGESZ = 6
AT_BASE = 7
AT_FLAGS = 8
AT_ENTRY = 9
AT_NOTELF = 10
AT_UID = 11
AT_EUID = 12
AT_GID = 13
AT_EGID = 14
AT_PLATFORM = 15
AT_HWCAP = 16
AT_CLKTCK = 17
AT_SECURE = 23
AT_BASE_PLATFORM = 24
AT_RANDOM = 25
AT_HWCAP2 = 26
AT_EXECFN = 31

FILE_DES = []

# start area memory for API hooking
# we will reserve 0x1000 bytes for this (which contains multiple slots of 4/8 bytes, each for one api)
API_HOOK_MEM = 0x1000000

# SYSCALL_MEM = 0xffff880000000000
# memory for syscall table
SYSCALL_MEM = API_HOOK_MEM + 0x1000



class ELFParse():
    def __init__(self, path, ql):
        self.path = os.path.abspath(path)
        self.ql = ql

        self.f = open(path, "rb")
        elfdata = self.f.read()
        self.elffile = ELFFile(self.f)

        self.elfdata = elfdata.ljust(52, b'\x00')        

        if self.elffile.e_ident_raw[ : 4] != b'\x7fELF':
            raise QlErrorELFFormat("[!] ERROR: NOT a ELF")

        self.elfhead = self.parse_header()
        if self.elfhead['e_type'] == "ET_REL":   # kernel driver
            self.is_driver = True
        else:
            self.is_driver = False


    def getelfdata(self, offest, size):
        return self.elfdata[offest : offest + size]
    
    def parse_header(self):
        return dict(self.elffile.header)

    def parse_sections(self):
        return self.elffile.iter_sections()

    def parse_segments(self):
        return self.elffile.iter_segments()


class QlLoaderELF(QlLoader, ELFParse):
    def __init__(self, ql):
        super(QlLoaderELF, self).__init__(ql)
        self.ql = ql
              
    def run(self):
        if self.ql.archbit == 32:
            stack_address = int(self.ql.os.profile.get("OS32", "stack_address"), 16)
            stack_size = int(self.ql.os.profile.get("OS32", "stack_size"), 16)
        elif self.ql.archbit == 64:
            stack_address = int(self.ql.os.profile.get("OS64", "stack_address"), 16)
            stack_size = int(self.ql.os.profile.get("OS64", "stack_size"), 16)

        if self.ql.shellcoder:
            self.ql.mem.map(self.ql.os.entry_point, self.ql.os.shellcoder_ram_size, info="[shellcode_stack]")
            self.ql.os.entry_point  = (self.ql.os.entry_point + 0x200000 - 0x1000)
            
            # for ASM file input, will mem.write in qltools
            try:
                self.ql.mem.write(self.ql.os.entry_point, self.ql.shellcoder)
            except:
                pass    
            
            self.ql.reg.arch_sp = self.ql.os.entry_point
            return
            
        self.path = self.ql.path
        ELFParse.__init__(self, self.path, self.ql)
        self.interp_address = 0
        self.mmap_address = 0
        self.ql.mem.map(stack_address, stack_size, info="[stack]")

        if self.ql.ostype == QL_OS.FREEBSD:
            init_rbp = stack_address + 0x40
            init_rdi = stack_address
            self.ql.reg.rbp = init_rbp
            self.ql.reg.rdi = init_rdi
            self.ql.reg.r14 = init_rdi


        if not self.is_driver:
            self.load_with_ld(stack_address + stack_size, argv = self.argv, env = self.env)
        else:
            
            # Linux kernel driver
            if self.load_driver(self.ql, stack_address + stack_size):
                raise QlErrorFileType("Unsupported FileType")
            # hook Linux kernel api
            self.ql.hook_code(hook_kernel_api)

        self.ql.reg.arch_sp = self.stack_address
        self.ql.os.stack_address  = self.stack_address


    # Copy strings to stack.
    def copy_str(self, addr, strs):
        l_addr = []
        s_addr = addr
        for s in strs:
            bs = s.encode("utf-8") + b"\x00"
            s_addr = s_addr - len(bs)
            # if isinstance(i, bytes):
            #   self.ql.nprint(type(b'\x00'))
            #   self.ql.nprint(type(i))
            #   self.ql.nprint(i)
            #   self.ql.nprint(type(i.encode()))
            #   self.ql.nprint(type(addr))
            #   self.ql.mem.write(s_addr, i + b'\x00')
            # else:
            self.ql.mem.write(s_addr, bs)
            l_addr.append(s_addr)
        return l_addr, s_addr


    def alignment(self, val):
        if self.ql.archbit == 64:
            return (val // 8) * 8
        elif self.ql.archbit == 32:
            return (val // 4) * 4


    def NEW_AUX_ENT(self, key, val):
        return self.ql.pack(int(key)) + self.ql.pack(int(val))    

    def NullStr(self, s):
        return s[ : s.find(b'\x00')]


    def load_with_ld(self, stack_addr, load_address = -1, argv = [], env = {}):
        if load_address <= 0:
            if self.ql.archbit == 64:
                load_address = int(self.ql.os.profile.get("OS64", "load_address"), 16)
            else:
                load_address = int(self.ql.os.profile.get("OS32", "load_address"), 16)

        elfhead = super().parse_header()

        # Determine the range of memory space opened up
        mem_start = -1
        mem_end = -1
        interp_path = ''
        for i in super().parse_segments():
            i = dict(i.header)
            if i['p_type'] == 'PT_LOAD':
                if mem_start > i['p_vaddr'] or mem_start == -1:
                    mem_start = i['p_vaddr']
                if mem_end < i['p_vaddr'] + i['p_memsz'] or mem_end == -1:
                    mem_end = i['p_vaddr'] + i['p_memsz']
            if i['p_type'] == 'PT_INTERP':
                interp_path = self.NullStr(super().getelfdata(i['p_offset'], i['p_filesz']))

        mem_start = int(mem_start // 0x1000) * 0x1000
        mem_end = int(mem_end // 0x1000 + 1) * 0x1000

        if elfhead['e_type'] == 'ET_EXEC':
            load_address = 0
        elif elfhead['e_type'] != 'ET_DYN':
            self.ql.dprint(D_INFO, "[+] Some error in head e_type: %i!", elfhead['e_type'])
            return -1

        for i in super().parse_segments():
            i = dict(i.header)
            if i['p_type'] == 'PT_LOAD':
                _mem_s = ((load_address + i["p_vaddr"]) // 0x1000 ) * 0x1000
                _mem_e = ((load_address + i["p_vaddr"] + i["p_filesz"]) // 0x1000 + 1) * 0x1000
                _perms = int(bin(i["p_flags"])[:1:-1], 2) # reverse bits for perms mapping

                self.ql.mem.map(_mem_s, _mem_e-_mem_s, perms=_perms, info=self.path)
                self.ql.dprint(D_INFO, "[+] load 0x%x - 0x%x" % (_mem_s, _mem_e))

                self.ql.mem.write(load_address+i["p_vaddr"], super().getelfdata(i['p_offset'], i['p_filesz']))

        loaded_mem_end = load_address + mem_end
        if loaded_mem_end > _mem_e:
            
            self.ql.mem.map(_mem_e, loaded_mem_end-_mem_e, info=self.path)
            self.ql.dprint(D_INFO, "[+] load 0x%x - 0x%x" % (_mem_e, loaded_mem_end)) # make sure we map all PT_LOAD tagged area

        entry_point = elfhead['e_entry'] + load_address
        self.ql.os.elf_mem_start = mem_start
        self.ql.dprint(D_INFO, "[+] mem_start: 0x%x mem_end: 0x%x" % (mem_start, mem_end))

        self.brk_address = mem_end + load_address + 0x2000

        # Load interpreter if there is an interpreter

        if interp_path != '':
            interp_path = str(interp_path, 'utf-8', errors="ignore")
           
            interp = ELFParse(self.ql.rootfs + interp_path, self.ql)
            interphead = interp.parse_header()
            self.ql.dprint(D_INFO, "[+] interp is : %s" % (self.ql.rootfs + interp_path))

            interp_mem_size = -1
            for i in interp.parse_segments():
                i =dict(i.header)
                if i['p_type'] == 'PT_LOAD':
                    if interp_mem_size < i['p_vaddr'] + i['p_memsz'] or interp_mem_size == -1:
                        interp_mem_size = i['p_vaddr'] + i['p_memsz']

            interp_mem_size = (interp_mem_size // 0x1000 + 1) * 0x1000
            self.ql.dprint(D_INFO, "[+] interp_mem_size is : 0x%x" % int(interp_mem_size))

            if self.ql.archbit == 64:
                self.interp_address = int(self.ql.os.profile.get("OS64", "interp_address"), 16)
            elif self.ql.archbit == 32:
                self.interp_address = int(self.ql.os.profile.get("OS32", "interp_address"), 16)

            self.ql.dprint(D_INFO, "[+] interp_address is : 0x%x" % (self.interp_address))
            self.ql.mem.map(self.interp_address, int(interp_mem_size), info=os.path.abspath(self.ql.rootfs + interp_path))

            for i in interp.parse_segments():
                # i =dict(i.header)
                if i['p_type'] == 'PT_LOAD':
                    self.ql.mem.write(self.interp_address + i['p_vaddr'], interp.getelfdata(i['p_offset'], i['p_filesz']))
            entry_point = interphead['e_entry'] + self.interp_address

        # Set MMAP addr
        if self.ql.archbit == 64:
            self.mmap_address = int(self.ql.os.profile.get("OS64", "mmap_address"), 16)
        else:
            self.mmap_address = int(self.ql.os.profile.get("OS32", "mmap_address"), 16)

        self.ql.dprint(D_INFO, "[+] mmap_address is : 0x%x" % (self.mmap_address))

        # Set elf table
        elf_table = b''
        new_stack = stack_addr

        # Set argc
        elf_table += self.ql.pack(len(argv))

        # Set argv
        if len(argv) != 0:
            argv_addr, new_stack = self.copy_str(stack_addr, argv)
            elf_table += b''.join([self.ql.pack(_) for _ in argv_addr])

        elf_table += self.ql.pack(0)

        # Set env
        if len(env) != 0:
            env_addr, new_stack = self.copy_str(new_stack, [key + '=' + value for key, value in env.items()])
            elf_table += b''.join([self.ql.pack(_) for _ in env_addr])    

        elf_table += self.ql.pack(0)

        new_stack = self.alignment(new_stack)
        randstr = 'a' * 0x10
        cpustr = 'i686'
        (addr, new_stack) = self.copy_str(new_stack, [randstr, cpustr])
        new_stack = self.alignment(new_stack)

        # Set AUX
        self.elf_phdr     = (load_address + elfhead['e_phoff'])
        self.elf_phent    = (elfhead['e_phentsize'])
        self.elf_phnum    = (elfhead['e_phnum'])
        self.elf_pagesz   = 0x1000
        self.elf_guid     = self.ql.os.uid
        self.elf_flags    = 0
        self.elf_entry    = (load_address + elfhead['e_entry'])
        self.randstraddr  = addr[0]
        self.cpustraddr   = addr[1]
        if self.ql.archbit == 64:
            self.elf_hwcap = 0x078bfbfd
        elif self.ql.archbit == 32:
            self.elf_hwcap = 0x1fb8d7
            if self.ql.archendian == QL_ENDIAN.EB:
                self.elf_hwcap = 0xd7b81f

        elf_table += self.NEW_AUX_ENT(AT_PHDR, self.elf_phdr + mem_start)
        elf_table += self.NEW_AUX_ENT(AT_PHENT, self.elf_phent)
        elf_table += self.NEW_AUX_ENT(AT_PHNUM, self.elf_phnum)
        elf_table += self.NEW_AUX_ENT(AT_PAGESZ, self.elf_pagesz)
        elf_table += self.NEW_AUX_ENT(AT_BASE, self.interp_address)
        elf_table += self.NEW_AUX_ENT(AT_FLAGS, self.elf_flags)
        elf_table += self.NEW_AUX_ENT(AT_ENTRY, self.elf_entry)
        elf_table += self.NEW_AUX_ENT(AT_UID, self.elf_guid)
        elf_table += self.NEW_AUX_ENT(AT_EUID, self.elf_guid)
        elf_table += self.NEW_AUX_ENT(AT_GID, self.elf_guid)
        elf_table += self.NEW_AUX_ENT(AT_EGID, self.elf_guid)
        elf_table += self.NEW_AUX_ENT(AT_HWCAP, self.elf_hwcap)
        elf_table += self.NEW_AUX_ENT(AT_CLKTCK, 100)
        elf_table += self.NEW_AUX_ENT(AT_RANDOM, self.randstraddr)
        elf_table += self.NEW_AUX_ENT(AT_PLATFORM, self.cpustraddr)
        elf_table += self.NEW_AUX_ENT(AT_SECURE, 0)
        elf_table += self.NEW_AUX_ENT(AT_NULL, 0)
        elf_table += b'\x00' * (0x10 - (new_stack - len(elf_table)) & 0xf)

        self.ql.mem.write(new_stack - len(elf_table), elf_table)
        new_stack = new_stack - len(elf_table)

        # self.ql.reg.write(UC_X86_REG_RDI, new_stack + 8)

        # for i in range(120):
        #     buf = self.ql.mem.read(new_stack + i * 0x8, 8)
        #     self.ql.nprint("0x%08x : 0x%08x " % (new_stack + i * 0x4, self.ql.unpack64(buf)) + ' '.join(['%02x' % i for i in buf]) + '  ' + ''.join([chr(i) if i in string.printable[ : -5].encode('ascii') else '.' for i in buf]))
        
        self.ql.os.entry_point = self.entry_point = entry_point
        self.ql.os.elf_entry = self.elf_entry = load_address + elfhead['e_entry']
        self.stack_address = new_stack
        self.load_address = load_address
        self.images.append(self.coverage_image(load_address, load_address + mem_end, self.path))
        self.ql.os.function_hook = FunctionHook(self.ql, self.elf_phdr + mem_start, self.elf_phnum, self.elf_phent, load_address, load_address + mem_end)
        self.init_sp = self.ql.reg.arch_sp

        # map vsyscall section for some specific needs
        if self.ql.archtype == QL_ARCH.X8664 and self.ql.ostype == QL_OS.LINUX:
            _vsyscall_addr = int(self.ql.os.profile.get("OS64", "vsyscall_address"), 16)
            _vsyscall_size = int(self.ql.os.profile.get("OS64", "vsyscall_size"), 16)

            if not self.ql.mem.is_mapped(_vsyscall_addr, _vsyscall_size):
                # initialize with \xcc then insert syscall entry
                # each syscall should be 1KiB(0x400 bytes) away
                self.ql.mem.map(_vsyscall_addr, _vsyscall_size, info="[vsyscall]")
                self.ql.mem.write(_vsyscall_addr, _vsyscall_size * b'\xcc')
                assembler = self.ql.create_assembler()

                def _compile(asm):
                    bs, _ = assembler.asm(asm)
                    return bytes(bs)

                _vsyscall_entry_asm = [ "mov rax, 0x60;",  # syscall gettimeofday
                                        "mov rax, 0xc9;",  # syscall time
                                        "mov rax, 0x135;", # syscall getcpu
                                       ]

                for idx, val in enumerate(_vsyscall_entry_asm):
                    self.ql.mem.write(_vsyscall_addr + idx * 0x400, _compile(val + "; syscall; ret"))
    # get file offset of init module function
    def lkm_get_init(self, ql):
        elffile = ELFFile(open(ql.path, 'rb'))
        symbol_tables = [s for s in elffile.iter_sections() if isinstance(s, SymbolTableSection)]
        for section in symbol_tables:
            for nsym, symbol in enumerate(section.iter_symbols()):
                if symbol.name == 'init_module':
                    addr = symbol.entry.st_value + elffile.get_section(symbol['st_shndx'])['sh_offset']
                    ql.nprint("init_module = 0x%x" %addr)
                    return addr

        # not found. FIXME: report error on invalid module??
        return -1


    def lkm_dynlinker(self, ql, mem_start):
        def get_symbol(elffile, name):
            section = elffile.get_section_by_name('.symtab')
            for symbol in section.iter_symbols():
                if symbol.name == name:
                    return symbol
            return None


        elffile = ELFFile(open(ql.path, 'rb'))

        all_symbols = []
        self.ql.os.hook_addr = API_HOOK_MEM
        # map address to symbol name
        ql.import_symbols = {}
        # reverse dictionary to map symbol name -> address
        rev_reloc_symbols = {}

        #dump_mem("XX Original code at 15a1 = ", ql.mem.read(0x15a1, 8))
        for section in elffile.iter_sections():
            # only care about reloc section
            if not isinstance(section, RelocationSection):
                continue

            # ignore reloc for module section
            if section.name == ".rela.gnu.linkonce.this_module":
                continue

            # The symbol table section pointed to in sh_link
            symtable = elffile.get_section(section['sh_link'])

            for rel in section.iter_relocations():
                if rel['r_info_sym'] == 0:
                    continue

                symbol = symtable.get_symbol(rel['r_info_sym'])

                # Some symbols have zero 'st_name', so instead what's used is
                # the name of the section they point at.
                if symbol['st_name'] == 0:
                    symsec = elffile.get_section(symbol['st_shndx']) # save sh_addr of this section
                    symbol_name = symsec.name
                    sym_offset = symsec['sh_offset']
                    # we need to do reverse lookup from symbol to address
                    rev_reloc_symbols[symbol_name] = sym_offset + mem_start
                else:
                    symbol_name = symbol.name
                    # get info about related section to be patched
                    info_section = elffile.get_section(section['sh_info'])
                    sym_offset = info_section['sh_offset']

                    if not symbol_name in all_symbols:
                        _symbol = get_symbol(elffile, symbol_name)
                        if _symbol['st_shndx'] == 'SHN_UNDEF':
                            # external symbol
                            # only save symbols of APIs
                            all_symbols.append(symbol_name)
                            # we need to lookup from address to symbol, so we can find the right callback
                            # for sys_xxx handler for syscall, the address must be aligned to 8
                            if symbol_name.startswith('sys_'):
                                if self.ql.os.hook_addr % self.ql.pointersize != 0:
                                    self.ql.os.hook_addr = (int(self.ql.os.hook_addr / self.ql.pointersize) + 1) * self.ql.pointersize
                                    # print("hook_addr = %x" %self.ql.os.hook_addr)
                            ql.import_symbols[self.ql.os.hook_addr] = symbol_name
                            # ql.nprint(":: Demigod is hooking %s(), at slot %x" %(symbol_name, self.ql.os.hook_addr))

                            if symbol_name == "page_offset_base":
                                # FIXME: this is for rootkit to scan for syscall table from page_offset_base
                                # write address of syscall table to this slot,
                                # so syscall scanner can quickly find it
                                ql.mem.write(self.ql.os.hook_addr, self.ql.pack(SYSCALL_MEM))

                            # we also need to do reverse lookup from symbol to address
                            rev_reloc_symbols[symbol_name] = self.ql.os.hook_addr
                            sym_offset = self.ql.os.hook_addr - mem_start
                            self.ql.os.hook_addr += self.ql.pointersize
                        else:
                            # local symbol
                            all_symbols.append(symbol_name)
                            _section = elffile.get_section(_symbol['st_shndx'])
                            rev_reloc_symbols[symbol_name] = _section['sh_offset'] + _symbol['st_value'] + mem_start
                            # ql.nprint(":: Add reverse lookup for %s to %x (%x, %x)" %(symbol_name, rev_reloc_symbols[symbol_name], _section['sh_offset'], _symbol['st_value']))
                            # ql.nprint(":: Add reverse lookup for %s to %x" %(symbol_name, rev_reloc_symbols[symbol_name]))
                    else:
                        sym_offset = rev_reloc_symbols[symbol_name] - mem_start

                # ql.nprint("Relocating symbol %s -> 0x%x" %(symbol_name, rev_reloc_symbols[symbol_name]))

                loc = elffile.get_section(section['sh_info'])['sh_offset'] + rel['r_offset']
                loc += mem_start

                if describe_reloc_type(rel['r_info_type'], elffile) == 'R_X86_64_32S':
                    # patch this reloc
                    if rel['r_addend']:
                        val = sym_offset + rel['r_addend']
                        val += mem_start
                        # ql.nprint('R_X86_64_32S %s: [0x%x] = 0x%x' %(symbol_name, loc, val & 0xFFFFFFFF))
                        ql.mem.write(loc, ql.pack32(val & 0xFFFFFFFF))
                    else:
                        # print("sym_offset = %x, rel = %x" %(sym_offset, rel['r_addend']))
                        # ql.nprint('R_X86_64_32S %s: [0x%x] = 0x%x' %(symbol_name, loc, rev_reloc_symbols[symbol_name] & 0xFFFFFFFF))
                        ql.mem.write(loc, ql.pack32(rev_reloc_symbols[symbol_name] & 0xFFFFFFFF))

                elif describe_reloc_type(rel['r_info_type'], elffile) == 'R_X86_64_64':
                    # patch this function?
                    val = sym_offset + rel['r_addend']
                    val += 0x2000000    # init_module position: FIXME
                    # finally patch this reloc
                    # ql.nprint('R_X86_64_64 %s: [0x%x] = 0x%x' %(symbol_name, loc, val))
                    ql.mem.write(loc, ql.pack64(val))

                elif describe_reloc_type(rel['r_info_type'], elffile) == 'R_X86_64_PC32':
                    # patch branch address: X86 case
                    val = rel['r_addend'] - loc
                    val += rev_reloc_symbols[symbol_name]
                    # finally patch this reloc
                    # ql.nprint('R_X86_64_PC32 %s: [0x%x] = 0x%x' %(symbol_name, loc, val & 0xFFFFFFFF))
                    ql.mem.write(loc, ql.pack32(val & 0xFFFFFFFF))

                elif describe_reloc_type(rel['r_info_type'], elffile) == 'R_386_PC32':
                    val = ql.unpack(ql.mem.read(loc, 4))
                    val = rev_reloc_symbols[symbol_name] + val - loc
                    ql.mem.write(loc, ql.pack32(val & 0xFFFFFFFF))

                elif describe_reloc_type(rel['r_info_type'], elffile) == 'R_386_32':
                    val = ql.unpack(ql.mem.read(loc, 4))
                    val = rev_reloc_symbols[symbol_name] + val
                    ql.mem.write(loc, ql.pack32(val & 0xFFFFFFFF))

        return rev_reloc_symbols


    def load_driver(self, ql, stack_addr, loadbase = 0):
        elfhead = super().parse_header()

        # Determine the range of memory space opened up
        mem_start = -1
        mem_end = -1

        # for i in super().parse_program_header(ql):
        #     if i['p_type'] == PT_LOAD:
        #         if mem_start > i['p_vaddr'] or mem_start == -1:
        #             mem_start = i['p_vaddr']
        #         if mem_end < i['p_vaddr'] + i['p_memsz'] or mem_end == -1:
        #             mem_end = i['p_vaddr'] + i['p_memsz']

        # mem_start = int(mem_start // 0x1000) * 0x1000
        # mem_end = int(mem_end // 0x1000 + 1) * 0x1000

        # FIXME
        mem_start = 0x1000
        mem_end = mem_start + int(len(self.elfdata) / 0x1000 + 1) * 0x1000

        # map some memory to intercept external functions of Linux kernel
        ql.mem.map(API_HOOK_MEM, 0x1000)

        # print("load addr = %x, size = %x" %(loadbase + mem_start, mem_end - mem_start))
        ql.mem.map(loadbase + mem_start, mem_end - mem_start)

        ql.nprint("[+] loadbase: %x, mem_start: %x, mem_end: %x" %(loadbase, mem_start, mem_end))

        ql.mem.write(loadbase + mem_start, self.elfdata)
        #dump_mem("Dumping some bytes:", self.elfdata[0x64 : 0x84])

        entry_point = self.lkm_get_init(ql) + loadbase + mem_start

        ql.brk_address = mem_end + loadbase

        # Set MMAP addr
        if self.ql.archbit == 64:
            self.mmap_address = int(self.ql.os.profile.get("OS64", "mmap_address"),16)
        else:
            self.mmap_address = int(self.ql.os.profile.get("OS32", "mmap_address"),16)

        self.ql.dprint(D_INFO, "[+] mmap_address is : 0x%x" % (self.mmap_address))

        new_stack = stack_addr
        new_stack = self.alignment(new_stack)

        # self.ql.os.elf_entry = self.elf_entry = loadbase + elfhead['e_entry']

        self.ql.os.entry_point = self.entry_point = entry_point
        self.elf_entry = self.ql.os.elf_entry = self.ql.os.entry_point

        self.stack_address = new_stack
        self.load_address = loadbase

        rev_reloc_symbols = self.lkm_dynlinker(ql, mem_start + loadbase)

        # remember address of syscall table, so external tools can access to it
        ql.os.syscall_addr = SYSCALL_MEM
        # setup syscall table
        ql.mem.map(SYSCALL_MEM, 0x1000)
        # zero out syscall table memory
        ql.mem.write(SYSCALL_MEM, b'\x00' * 0x1000)

        #print("sys_close = %x" %rev_reloc_symbols['sys_close'])
        # print(rev_reloc_symbols.keys())
        for sc in rev_reloc_symbols.keys():
            if sc != 'sys_call_table' and sc.startswith('sys_'):
                tmp_sc = sc.replace("sys_", "NR_")
                if tmp_sc in globals():
                    syscall_id = globals()[tmp_sc]
                    print("Writing syscall %s to [0x%x]" %(sc, SYSCALL_MEM + ql.pointersize * syscall_id))
                    ql.mem.write(SYSCALL_MEM + ql.pointersize * syscall_id, ql.pack(rev_reloc_symbols[sc]))

        # write syscall addresses into syscall table
        #ql.mem.write(SYSCALL_MEM + 0, struct.pack("<Q", hook_sys_read))
        ql.mem.write(SYSCALL_MEM + 0, ql.pack(self.ql.os.hook_addr))
        #ql.mem.write(SYSCALL_MEM + 1  * 8, struct.pack("<Q", hook_sys_write))
        ql.mem.write(SYSCALL_MEM + 1  * ql.pointersize, ql.pack(self.ql.os.hook_addr + 1 * ql.pointersize))
        #ql.mem.write(SYSCALL_MEM + 2  * 8, struct.pack("<Q", hook_sys_open))
        ql.mem.write(SYSCALL_MEM + 2  * ql.pointersize, ql.pack(self.ql.os.hook_addr + 2 * ql.pointersize))

        # setup hooks for read/write/open syscalls
        ql.import_symbols[self.ql.os.hook_addr] = hook_sys_read
        ql.import_symbols[self.ql.os.hook_addr + 1 * ql.pointersize] = hook_sys_write
        ql.import_symbols[self.ql.os.hook_addr + 2 * ql.pointersize] = hook_sys_open
