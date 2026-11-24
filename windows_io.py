import ctypes
import os
import time
from ctypes import wintypes


class PdhValue(ctypes.Structure):
    _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]


class DiskCounters:
    def __init__(self, drive=None):
        self.pdh = ctypes.WinDLL("pdh")
        self.query = ctypes.c_void_p()
        self.counters = {}
        if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
            return

        drive = drive.upper().rstrip(":") + ":" if drive else os.environ.get("SystemDrive", "C:")
        for name in ("% Idle Time", "Disk Bytes/sec"):
            handle = ctypes.c_void_p()
            path = fr"\LogicalDisk({drive})\{name}"
            if self.pdh.PdhAddEnglishCounterW(self.query, ctypes.c_wchar_p(path), 0, ctypes.byref(handle)) == 0:
                self.counters[name] = handle
        self.pdh.PdhCollectQueryData(self.query)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        if self.query.value:
            self.pdh.PdhCloseQuery(self.query)

    def read(self):
        if not self.counters or self.pdh.PdhCollectQueryData(self.query) != 0:
            return None, None

        values = {}
        for name, handle in self.counters.items():
            result = PdhValue()
            status = self.pdh.PdhGetFormattedCounterValue(handle, 0x200, None, ctypes.byref(result))
            if status == 0 and result.status in (0, 1):
                values[name] = result.value
        idle = values.get("% Idle Time")
        return (None if idle is None else min(100, max(0, 100 - idle)),
                values.get("Disk Bytes/sec"))


class InterfaceRow(ctypes.Structure):
    # MIB_IF_ROW2 from netioapi.h. GUIDs are 16 bytes with 4-byte alignment.
    _fields_ = [
        ("luid", ctypes.c_uint64), ("index", wintypes.ULONG), ("guid", wintypes.DWORD * 4),
        ("alias", wintypes.WCHAR * 257), ("description", wintypes.WCHAR * 257),
        ("address_length", wintypes.ULONG), ("address", ctypes.c_ubyte * 32),
        ("permanent_address", ctypes.c_ubyte * 32), ("mtu", wintypes.ULONG),
        ("type", wintypes.ULONG), ("tunnel_type", ctypes.c_int), ("media_type", ctypes.c_int),
        ("physical_medium", ctypes.c_int), ("access_type", ctypes.c_int), ("direction", ctypes.c_int),
        ("flags", ctypes.c_ubyte), ("oper_status", ctypes.c_int), ("admin_status", ctypes.c_int),
        ("media_connect_state", ctypes.c_int), ("network_guid", wintypes.DWORD * 4),
        ("connection_type", ctypes.c_int), ("tx_speed", ctypes.c_uint64),
        ("rx_speed", ctypes.c_uint64), ("in_octets", ctypes.c_uint64),
        ("other_in_counters", ctypes.c_uint64 * 8), ("out_octets", ctypes.c_uint64),
        ("other_out_counters", ctypes.c_uint64 * 8),
    ]


class InterfaceTable(ctypes.Structure):
    _fields_ = [("count", wintypes.ULONG), ("rows", InterfaceRow * 1)]


class NetworkCounters:
    def __init__(self):
        self.ip_helper = ctypes.WinDLL("iphlpapi")
        self.ip_helper.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(InterfaceTable))]
        self.ip_helper.FreeMibTable.argtypes = [ctypes.c_void_p]
        self.ip_helper.FreeMibTable.restype = None
        self.previous = self.snapshot()
        self.sampled_at = time.monotonic()

    def snapshot(self):
        table = ctypes.POINTER(InterfaceTable)()
        if self.ip_helper.GetIfTable2(ctypes.byref(table)) != 0:
            return None
        try:
            first_row = ctypes.addressof(table.contents) + InterfaceTable.rows.offset
            rows = (InterfaceRow * table.contents.count).from_address(first_row)
            # Hardware Ethernet and Wi-Fi interfaces that are operationally up.
            return {row.luid: (row.in_octets, row.out_octets) for row in rows
                    if row.flags & 1 and row.type in (6, 71) and row.oper_status == 1}
        finally:
            self.ip_helper.FreeMibTable(table)

    def read(self):
        current = self.snapshot()
        now = time.monotonic()
        previous, elapsed = self.previous, now - self.sampled_at
        self.previous, self.sampled_at = current, now
        if current is None or previous is None or elapsed <= 0:
            return None, None

        received = sent = 0
        for luid in current.keys() & previous.keys():
            incoming, outgoing = current[luid]
            old_incoming, old_outgoing = previous[luid]
            received += max(0, incoming - old_incoming)
            sent += max(0, outgoing - old_outgoing)
        return received / elapsed, sent / elapsed
