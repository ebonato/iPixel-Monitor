import ctypes
import logging
import winreg
from ctypes import wintypes
from pathlib import Path
from threading import Lock


LOGGER = logging.getLogger("ipixel.pawnio")
DWORD_PTR = ctypes.POINTER(ctypes.c_uint64)
IA32_TEMPERATURE_TARGET = 0x1A2
IA32_PACKAGE_THERM_STATUS = 0x1B1
AMD_TCTL_REGISTER = 0x59800


def registry_string(key, name):
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
        return winreg.QueryValueEx(handle, name)[0]


def pawnio_paths():
    program_files = Path(registry_string(r"SOFTWARE\Microsoft\Windows\CurrentVersion", "ProgramFilesDir"))
    return program_files / "PawnIO" / "PawnIOLib.dll", program_files / "iPixel-Monitor" / "PawnIO-Modules"


def intel_cpu_package(target, status):
    tj_max = (target >> 16) & 0xFF
    if not 70 <= tj_max <= 150 or not status & (1 << 31):
        return None
    value = tj_max - ((status >> 16) & 0x7F)
    return value if 0 < value <= 150 else None


def amd_cpu_tctl(status):
    raw = status & 0xFFFFFFFF
    value = (raw >> 21) * 0.125
    if raw & (1 << 19) or raw & (3 << 16) == 3 << 16:
        value -= 49
    return value if 0 < value <= 150 else None


class PawnIOTemperature:
    """Read Intel CPU Package or AMD Zen Tctl through official signed PawnIO modules."""

    def __init__(self, modules=None):
        vendor = registry_string(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "VendorIdentifier")
        module_name = {"GenuineIntel": "IntelMSR.bin", "AuthenticAMD": "AMDFamily17.bin"}.get(vendor)
        if not module_name:
            raise ValueError(f"CPU sem módulo térmico PawnIO compatível: {vendor}")
        self.intel = vendor == "GenuineIntel"
        dll_path, default_modules = pawnio_paths()
        module_folder = modules if modules is not None else default_modules
        module_path = module_folder / module_name
        if not module_path.is_file() or not dll_path.is_file():
            raise FileNotFoundError("Prepare o PawnIO com preparar_pawnio.py ou confira a pasta dos módulos.")
        self.blob = module_path.read_bytes()
        self.api = ctypes.WinDLL(str(dll_path))
        self.api.pawnio_open.argtypes = [ctypes.POINTER(wintypes.HANDLE)]
        self.api.pawnio_load.argtypes = [wintypes.HANDLE, ctypes.c_char_p, ctypes.c_size_t]
        self.api.pawnio_execute.argtypes = [
            wintypes.HANDLE, ctypes.c_char_p, DWORD_PTR, ctypes.c_size_t,
            DWORD_PTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        self.api.pawnio_close.argtypes = [wintypes.HANDLE]
        self.handle = wintypes.HANDLE()
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.pci_mutex = None
        self.lock = Lock()
        self.last_error = None

    def __enter__(self):
        self.check(self.api.pawnio_open(ctypes.byref(self.handle)), "Acesso ao PawnIO")
        try:
            self.check(self.api.pawnio_load(self.handle, self.blob, len(self.blob)), "Carregamento do módulo PawnIO")
            if not self.intel:
                self.pci_mutex = self.kernel.CreateMutexW(None, False, r"Global\Access_PCI")
                if not self.pci_mutex:
                    raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self.close()
            raise
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        with self.lock:
            if self.pci_mutex:
                self.kernel.CloseHandle(self.pci_mutex)
                self.pci_mutex = None
            if self.handle.value:
                self.api.pawnio_close(self.handle)
                self.handle.value = None

    @staticmethod
    def check(status, operation):
        if status < 0:
            raise OSError(f"{operation} falhou (0x{status & 0xFFFFFFFF:08X}).")

    def read_register(self, operation, address):
        input_value = ctypes.c_uint64(address)
        output_value = ctypes.c_uint64()
        count = ctypes.c_size_t()
        result = self.api.pawnio_execute(self.handle, operation, ctypes.byref(input_value), 1,
                                         ctypes.byref(output_value), 1, ctypes.byref(count))
        self.check(result, "Leitura térmica PawnIO")
        if count.value != 1:
            raise OSError("PawnIO não retornou um valor.")
        return output_value.value

    def read_tctl(self):
        acquired = self.kernel.WaitForSingleObject(self.pci_mutex, 100)
        if acquired not in (0, 0x80):
            return None
        try:
            return amd_cpu_tctl(self.read_register(b"ioctl_read_smn", AMD_TCTL_REGISTER))
        finally:
            self.kernel.ReleaseMutex(self.pci_mutex)

    def read(self):
        with self.lock:
            try:
                if self.intel:
                    target = self.read_register(b"ioctl_read_msr", IA32_TEMPERATURE_TARGET)
                    status = self.read_register(b"ioctl_read_msr", IA32_PACKAGE_THERM_STATUS)
                    value = intel_cpu_package(target, status)
                else:
                    value = self.read_tctl()
                if value is None:
                    raise OSError("Valor térmico indisponível.")
            except OSError as error:
                reason = str(error)
                if reason != self.last_error:
                    LOGGER.warning("%s", reason)
                self.last_error = reason
                return None
            self.last_error = None
            return value
