import logging
import struct
from typing import Callable, Dict, Tuple, Any, Optional

from unicorn import (  # type: ignore
    Uc, UcError, UC_ARCH_X86, UC_MODE_32, UC_MODE_64, UC_PROT_READ,
    UC_PROT_WRITE, UC_PROT_ALL, UC_HOOK_MEM_UNMAPPED, UC_HOOK_BLOCK)
from unicorn.x86_const import (  # type: ignore
    UC_X86_REG_ESP, UC_X86_REG_EBP, UC_X86_REG_EIP, UC_X86_REG_RSP,
    UC_X86_REG_RBP, UC_X86_REG_RIP, UC_X86_REG_MSR, UC_X86_REG_EAX,
    UC_X86_REG_RAX)

from .dump_utils import pointer_size_to_fmt
from .process_control import ProcessController, Architecture, ReadProcessMemoryError

STACK_MAGIC_RET_ADDR = 0xdeadbeef
LOG = logging.getLogger(__name__)


def resolve_wrapped_api(
        wrapper_start_addr: int,
        process_controller: ProcessController,
        expected_ret_addr: Optional[int] = None) -> Optional[int]:
    arch = process_controller.architecture
    if arch == Architecture.X86_32:
        uc_arch = UC_ARCH_X86
        uc_mode = UC_MODE_32
        pc_register = UC_X86_REG_EIP
        sp_register = UC_X86_REG_ESP
        bp_register = UC_X86_REG_EBP
        result_register = UC_X86_REG_EAX
        stack_addr = 0xff000000
        setup_teb = _setup_teb_x86
    elif arch == Architecture.X86_64:
        uc_arch = UC_ARCH_X86
        uc_mode = UC_MODE_64
        pc_register = UC_X86_REG_RIP
        sp_register = UC_X86_REG_RSP
        bp_register = UC_X86_REG_RBP
        result_register = UC_X86_REG_RAX
        stack_addr = 0xff00000000000000
        setup_teb = _setup_teb_x64
    else:
        raise NotImplementedError(f"Architecture '{arch}' isn't supported")

    uc = Uc(uc_arch, uc_mode)
    try:
        # Map fake return address's page in case wrappers try to access it
        aligned_addr = STACK_MAGIC_RET_ADDR - (STACK_MAGIC_RET_ADDR %
                                               process_controller.page_size)
        uc.mem_map(aligned_addr, process_controller.page_size, UC_PROT_ALL)

        # Setup a stack
        stack_size = 3 * process_controller.page_size
        stack_start = stack_addr + stack_size - process_controller.page_size
        uc.mem_map(stack_addr, stack_size, UC_PROT_READ | UC_PROT_WRITE)
        uc.mem_write(
            stack_start,
            struct.pack(pointer_size_to_fmt(process_controller.pointer_size),
                        STACK_MAGIC_RET_ADDR))
        uc.reg_write(sp_register, stack_start)
        uc.reg_write(bp_register, stack_start)

        # Setup FS/GSBASE
        setup_teb(uc, process_controller)

        # Setup hooks
        if expected_ret_addr is None:
            stop_on_ret_addr = STACK_MAGIC_RET_ADDR
        else:
            stop_on_ret_addr = expected_ret_addr
        uc.hook_add(UC_HOOK_MEM_UNMAPPED,
                    _unicorn_hook_unmapped,
                    user_data=process_controller)
        uc.hook_add(UC_HOOK_BLOCK,
                    _unicorn_hook_block,
                    user_data=(process_controller, stop_on_ret_addr))

        uc.emu_start(wrapper_start_addr, wrapper_start_addr + 1024)

        # Read and return PC
        pc = uc.reg_read(result_register)
        assert isinstance(pc, int)

        return pc
    except UcError as e:
        LOG.debug("ERROR: %s", str(e))
        pc = uc.reg_read(pc_register)
        assert isinstance(pc, int)
        sp = uc.reg_read(sp_register)
        assert isinstance(sp, int)
        bp = uc.reg_read(bp_register)
        assert isinstance(bp, int)
        LOG.debug("PC=%s", hex(pc))
        LOG.debug("SP=%s", hex(sp))
        LOG.debug("BP=%s", hex(bp))
        return None


def _setup_teb_x86(uc: Uc, process_info: ProcessController) -> None:
    MSG_IA32_FS_BASE = 0xC0000100
    teb_addr = 0xff100000
    peb_addr = 0xff200000
    # Map tables
    uc.mem_map(teb_addr, process_info.page_size, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_map(peb_addr, process_info.page_size, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_write(teb_addr + 0x18, struct.pack(pointer_size_to_fmt(4),
                                              teb_addr))
    uc.mem_write(teb_addr + 0x30, struct.pack(pointer_size_to_fmt(4),
                                              peb_addr))
    uc.reg_write(UC_X86_REG_MSR, (MSG_IA32_FS_BASE, teb_addr))


def _setup_teb_x64(uc: Uc, process_info: ProcessController) -> None:
    MSG_IA32_GS_BASE = 0xC0000101
    teb_addr = 0xff10000000000000
    peb_addr = 0xff20000000000000
    # Map tables
    uc.mem_map(teb_addr, process_info.page_size, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_map(peb_addr, process_info.page_size, UC_PROT_READ | UC_PROT_WRITE)
    uc.mem_write(teb_addr + 0x30, struct.pack(pointer_size_to_fmt(8),
                                              teb_addr))
    uc.mem_write(teb_addr + 0x60, struct.pack(pointer_size_to_fmt(8),
                                              peb_addr))
    uc.reg_write(UC_X86_REG_MSR, (MSG_IA32_GS_BASE, teb_addr))


def _unicorn_hook_unmapped(uc: Uc, _access: Any, address: int, _size: int,
                           _value: int,
                           process_controller: ProcessController) -> bool:
    LOG.debug("Unmapped memory at %s", hex(address))
    if address == 0:
        return False

    page_size = process_controller.page_size
    aligned_addr = address - (address & (page_size - 1))
    try:
        in_process_data = process_controller.read_process_memory(
            aligned_addr, page_size)
        uc.mem_map(aligned_addr, len(in_process_data), UC_PROT_ALL)
        uc.mem_write(aligned_addr, in_process_data)
        LOG.debug("Mapped %d bytes at %s", len(in_process_data),
                  hex(aligned_addr))
        return True
    except UcError as e:
        LOG.error("ERROR: %s", str(e))
        return False
    except ReadProcessMemoryError as e:
        # Log this error as debug as it's expected to happen in cases where we
        # reach the end of the IAT.
        LOG.debug("ERROR: %s", str(e))
        return False
    except Exception as e:
        LOG.error("ERROR: %s", str(e))
        return False


def _unicorn_hook_block(uc: Uc, address: int, _size: int,
                        user_data: Tuple[ProcessController, int]) -> None:
    process_controller, stop_on_ret_addr = user_data
    ptr_size = process_controller.pointer_size
    arch = process_controller.architecture
    if arch == Architecture.X86_32:
        pc_register = UC_X86_REG_EIP
        sp_register = UC_X86_REG_ESP
        result_register = UC_X86_REG_EAX
    elif arch == Architecture.X86_64:
        pc_register = UC_X86_REG_RIP
        sp_register = UC_X86_REG_RSP
        result_register = UC_X86_REG_RAX
    else:
        raise NotImplementedError(f"Unsupported architecture: {arch}")

    exports_dict = process_controller.enumerate_exported_functions()
    if address in exports_dict:
        # Reached an export or returned to the call site
        sp = uc.reg_read(sp_register)
        assert isinstance(sp, int)
        ret_addr_data = uc.mem_read(sp, ptr_size)
        ret_addr = struct.unpack(pointer_size_to_fmt(ptr_size),
                                 ret_addr_data)[0]
        api_name = exports_dict[address]['name']
        LOG.debug("Reached API '%s'", api_name)
        if ret_addr == stop_on_ret_addr or \
            ret_addr == stop_on_ret_addr + 1 \
                or ret_addr == STACK_MAGIC_RET_ADDR:
            # Most wrappers should end up here directly
            uc.reg_write(result_register, address)
            uc.emu_stop()
            return
        if _is_no_return_api(api_name):
            # Note: Dirty fix for ExitProcess-like wrappers on WinLicense 3.x
            LOG.debug("Reached noreturn API, stopping emulation")
            uc.reg_write(result_register, address)
            uc.emu_stop()
            return
        if _is_bogus_api(api_name):
            # Note: Starting with Themida 3.1.4.0, wrappers call some useless
            # APIs to fool emulation-based unwrappers
            LOG.debug("Reached bogus API call, skipping")
            # "Simulate" bogus call
            result, arg_count = _simulate_bogus_api(api_name)
            # Set result
            uc.reg_write(result_register, result)

            # Fix the stack
            if arch == Architecture.X86_32:
                # Pop return address and arguments from the stack
                uc.reg_write(sp_register, sp + ptr_size * (1 + arg_count))
            elif arch == Architecture.X86_64:
                # Pop return address and arguments from the stack
                stack_arg_count = max(0, arg_count - 4)
                uc.reg_write(sp_register,
                             sp + ptr_size * (1 + stack_arg_count))

            # Set next address
            uc.reg_write(pc_register, ret_addr)
            return


def _is_no_return_api(api_name: str) -> bool:
    NO_RETURN_APIS = ["ExitProcess", "FatalExit", "ExitThread"]
    return api_name in NO_RETURN_APIS


def _is_bogus_api(api_name: str) -> bool:
    BOGUS_APIS = ["Sleep"]
    return api_name in BOGUS_APIS


def _simulate_bogus_api(api_name: str) -> Tuple[int, int]:
    BOGUS_API_MAP: Dict[str, Tuple[int, int]] = {
        "Sleep": (0, 1),
    }
    return BOGUS_API_MAP[api_name]
