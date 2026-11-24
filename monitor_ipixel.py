import argparse
import asyncio
import ctypes
import io
import json
import logging
import math
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from contextlib import ExitStack
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.request import urlopen

from bleak.exc import BleakError
from PIL import Image, ImageDraw
from pypixelcolor import AsyncClient

from dell_temperature import DellTemperature
from windows_io import DiskCounters, NetworkCounters

SIZE = (96, 16)
DEFAULT_LHM_URL = "http://127.0.0.1:8085/data.json"
LOGGER = logging.getLogger("ipixel")
FILLED = "#eff6ff"
BACKGROUND = "#020613"
COLORS = ("#26dcff", "#fa70ef", "#7bff9d")
NETWORK_COLORS = ("#62f2ff", "#ffc04f")
GLYPHS = {
    "0": (7, 5, 5, 5, 7), "1": (2, 6, 2, 2, 7),
    "2": (7, 1, 7, 4, 7), "3": (7, 1, 7, 1, 7),
    "4": (5, 5, 7, 1, 1), "5": (7, 4, 7, 1, 7),
    "6": (7, 4, 7, 5, 7), "7": (7, 1, 2, 2, 2),
    "8": (7, 5, 7, 5, 7), "9": (7, 5, 7, 1, 7),
    "A": (2, 5, 7, 5, 5), "C": (7, 4, 4, 4, 7),
    "B": (6, 5, 6, 5, 6), "D": (6, 5, 5, 5, 6),
    "E": (7, 4, 6, 4, 7), "G": (7, 4, 5, 5, 7),
    "I": (7, 2, 2, 2, 7), "K": (5, 5, 6, 5, 5), "M": (5, 7, 7, 5, 5),
    "O": (7, 5, 5, 5, 7), "P": (7, 5, 7, 4, 4),
    "R": (6, 5, 6, 5, 5), "S": (7, 4, 7, 1, 7),
    "T": (7, 2, 2, 2, 2), "U": (5, 5, 5, 5, 7),
    "V": (5, 5, 5, 5, 2), "%": (5, 1, 2, 4, 5),
    "-": (0, 0, 7, 0, 0), ".": (0, 0, 0, 0, 2),
    "↓": (2, 2, 2, 7, 2), "↑": (2, 7, 2, 2, 2),
    "°": (7, 5, 7, 0, 0),
}


def find_nvidia_smi():
    if not ctypes.windll.shell32.IsUserAnAdmin():
        return shutil.which("nvidia-smi")

    # An elevated task must not execute programs found in the user's PATH.
    system_directory = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(system_directory, len(system_directory))
    if 0 < length < len(system_directory):
        candidate = Path(system_directory.value) / "nvidia-smi.exe"
        if candidate.is_file():
            return str(candidate)
    return None


class MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.DWORD), ("load", wintypes.DWORD),
        ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
        ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
        ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
        ("available_extended", ctypes.c_ulonglong),
    ]


@dataclass(frozen=True)
class Metrics:
    cpu: float
    cpu_temp: float | None
    gpu: float | None
    gpu_temp: float | None
    ram: float
    ram_gib: float
    vram_used: float | None
    vram_total: float | None
    disk: float | None
    disk_bytes_per_second: float | None
    download: float | None
    upload: float | None


class WindowsMetrics:
    def __init__(self, lhm_url, disk, gpu_index, cpu_sensor=None):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME)] * 3
        self.kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
        self.last_cpu = self.cpu_times()
        self.disk = disk
        self.network = NetworkCounters()
        self.nvidia_smi = find_nvidia_smi()
        self.gpu_index = gpu_index
        self.lhm_url = lhm_url
        self.cpu_sensor = cpu_sensor

    def cpu_times(self):
        idle, kernel, user = (wintypes.FILETIME() for _ in range(3))
        if not self.kernel.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        return tuple(value.dwHighDateTime << 32 | value.dwLowDateTime for value in (idle, kernel, user))

    def read(self):
        current = self.cpu_times()
        idle, kernel, user = (now - previous for now, previous in zip(current, self.last_cpu))
        self.last_cpu = current
        total = kernel + user
        cpu = 100 * (total - idle) / total if total > 0 else 0

        ram = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
        if not self.kernel.GlobalMemoryStatusEx(ctypes.byref(ram)):
            raise ctypes.WinError(ctypes.get_last_error())
        disk, disk_bytes_per_second = self.disk.read()
        download, upload = self.network.read()
        gpu, gpu_temp, vram_used, vram_total = self.read_nvidia()
        sensors = read_lhm(self.lhm_url) if self.lhm_url else {}
        cpu_temp = self.cpu_sensor.read() if self.cpu_sensor else None
        return Metrics(
            cpu, cpu_temp if cpu_temp is not None else sensors.get("cpu_temp"),
            gpu if gpu is not None else sensors.get("gpu"),
            gpu_temp if gpu_temp is not None else sensors.get("gpu_temp"),
            ram.load, (ram.total - ram.available) / 2**30,
            vram_used, vram_total, disk, disk_bytes_per_second, download, upload,
        )

    def read_nvidia(self):
        if self.nvidia_smi:
            try:
                output = subprocess.check_output(
                    [self.nvidia_smi, f"--id={self.gpu_index}",
                     "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    text=True, timeout=2, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                parts = output.strip().split(",")
                if len(parts) == 4:
                    return tuple(read_number(part) for part in parts)
            except (OSError, subprocess.SubprocessError):
                pass
        return None, None, None, None


def read_number(value):
    try:
        number = float(str(value).strip().replace(",", ".").split()[0])
        return number if math.isfinite(number) else None
    except (ValueError, IndexError):
        return None


def sensor_nodes(node):
    if node.get("SensorId"):
        yield node
    for child in node.get("Children", []):
        yield from sensor_nodes(child)


def read_lhm(url):
    try:
        with urlopen(url, timeout=0.8) as response:
            tree = json.load(response)
    except (OSError, ValueError):
        return {}
    matches = {}
    for sensor in sensor_nodes(tree):
        identifier = sensor["SensorId"].lower().split("/")
        name = sensor.get("Text", "").lower()
        kind = sensor.get("Type", "").lower()
        value = read_number(sensor.get("RawValue", sensor.get("Value")))
        if len(identifier) < 2 or value is None:
            continue
        device = identifier[1]
        if kind == "temperature" and device in ("intelcpu", "amdcpu"):
            priorities = {"cpu package": 0, "cpu (tctl/tdie)": 1, "core (tctl/tdie)": 2,
                          "core average": 3, "core (tdie)": 4}
            priority = priorities.get(name)
            if priority is not None and priority < matches.get("cpu_temp", (99, None))[0]:
                matches["cpu_temp"] = (priority, value)
        if device.startswith("gpu") and name in ("gpu core", "gpu total"):
            key = {"temperature": "gpu_temp", "load": "gpu"}.get(kind)
            if key:
                matches.setdefault(key, (0, value))
    return {name: value for name, (_, value) in matches.items()}


def pixel_text(draw, x, y, text, color):
    for index, char in enumerate(text):
        for row, mask in enumerate(GLYPHS.get(char, ())):
            for column in range(3):
                if mask & (4 >> column):
                    draw.point((x + index * 4 + column, y + row), fill=color)


def percent(value):
    return "--" if value is None else f"{max(0, min(100, round(value)))}%"


def temperature(value):
    if value is None:
        return "--°", "#738399"
    hue = "#ff5468" if value >= 85 else "#ffc04f" if value >= 70 else "#86edba"
    return f"{round(value)}°", hue


def space(value):
    if value is None:
        return "--G"
    if value < 9.95:
        return f"{value:.1f}G"
    if value < 999.5:
        return f"{value:.0f}G"
    return f"{value / 1024:.0f}T"


def rate(value):
    if value is None:
        return "--"
    for multiplier, suffix in ((10**9, "G"), (10**6, "M"), (1000, "K"), (1, "B")):
        if value >= multiplier * 0.9995 or multiplier == 1:
            scaled = value / multiplier
            return f"{scaled:.1f}{suffix}" if multiplier > 1 and scaled < 9.95 else f"{round(scaled)}{suffix}"


def card_heading(draw, index, title):
    x = index * 32
    accent = COLORS[index]
    draw.rectangle((x, 0, x + 30, 5), fill="#121d36")
    draw.line((x, 0, x, 5), fill=accent)
    pixel_text(draw, x + 3, 0, title, accent)
    return x, accent


def bar(draw, x, level, accent, segments):
    for segment in range(segments):
        start = x + segment * 4
        draw.rectangle((start, 13, start + 2, 15), fill="#1b2741")
        if level is not None:
            width = min(3, max(0, round((level / 100 * segments - segment) * 3)))
            if width:
                draw.rectangle((start, 13, start + width - 1, 15), fill=accent)


def card(draw, index, title, reading, detail, level, detail_color=FILLED):
    x, accent = card_heading(draw, index, title)
    pixel_text(draw, x, 7, reading, FILLED)
    if detail:
        pixel_text(draw, x + 32 - 4 * len(detail), 7, detail, detail_color)
    bar(draw, x, level, accent, 8)


def network_card(draw, download, upload):
    x, _ = card_heading(draw, 2, "REDE")
    for index, (value, color, arrow) in enumerate(zip((download, upload), NETWORK_COLORS, ("↓", "↑"))):
        column = x + 16 * index
        pixel_text(draw, x + 21 + 5 * index, 0, arrow, color)
        pixel_text(draw, column, 7, rate(value), color)
        level = None if value is None else 100 * math.log1p(max(0, value) / 1000) / math.log1p(100_000)
        bar(draw, column, level, color, 4)


def frame(metrics, page):
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    if page == "principal":
        cpu_temp, cpu_color = temperature(metrics.cpu_temp)
        gpu_temp, gpu_color = temperature(metrics.gpu_temp)
        card(draw, 0, "CPU", percent(metrics.cpu), cpu_temp, metrics.cpu, cpu_color)
        card(draw, 1, "GPU", percent(metrics.gpu), gpu_temp, metrics.gpu, gpu_color)
        card(draw, 2, "RAM", percent(metrics.ram), space(metrics.ram_gib), metrics.ram, COLORS[2])
    else:
        vram_pct = (100 * metrics.vram_used / metrics.vram_total
                    if metrics.vram_used is not None and metrics.vram_total else None)
        vram_gib = metrics.vram_used / 1024 if metrics.vram_used is not None else None
        card(draw, 0, "VRAM", percent(vram_pct), space(vram_gib), vram_pct)
        card(draw, 1, "DISCO", percent(metrics.disk), rate(metrics.disk_bytes_per_second), metrics.disk)
        network_card(draw, metrics.download, metrics.upload)
    return image


async def run(args):
    if args.scan:
        devices = await AsyncClient.scan(timeout=args.scan_seconds)
        for device in sorted(devices, key=lambda device: (device.name, device.address)):
            LOGGER.info("%s  %s", device.address, device.name)
        if not devices:
            LOGGER.info("Nenhum painel LED encontrado. Ligue o painel e desconecte o aplicativo do celular.")
        return

    with ExitStack() as resources:
        disk = resources.enter_context(DiskCounters(args.disk))
        cpu_sensor = None
        if args.dell_cpu_sensor is not None:
            cpu_sensor = DellTemperature(args.dell_cpu_sensor)
        elif args.pawnio or args.pawnio_modules is not None:
            from pawnio_temperature import PawnIOTemperature
            cpu_sensor = resources.enter_context(PawnIOTemperature(args.pawnio_modules))
        metrics = WindowsMetrics(args.lhm_url, disk, args.gpu_index, cpu_sensor)
        await asyncio.sleep(0.5)
        if args.preview:
            image = frame(await asyncio.to_thread(metrics.read), args.preview_page)
            image.resize((768, 128), Image.Resampling.NEAREST).save(args.preview)
        else:
            await display(args, metrics)


async def display(args, metrics):
    auto = args.page == "auto" and not args.once
    while True:
        try:
            async with AsyncClient(args.address) as panel:
                info = panel.get_device_info()
                if (info.width, info.height) != SIZE:
                    raise ValueError(f"Painel detectado: {info.width}x{info.height}. Esperado: 96x16.")
                LOGGER.info("Painel conectado.")
                page = "principal" if args.page == "auto" else args.page
                deadline = math.inf
                last_page = None
                previous = None
                while True:
                    started = time.monotonic()
                    sample = await asyncio.to_thread(metrics.read)
                    if auto and time.monotonic() >= deadline:
                        page = "detalhes" if page == "principal" else "principal"
                    image = frame(sample, page)
                    pixels = image.tobytes()
                    if pixels != previous:
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG")
                        await panel.send_image_hex(buffer.getvalue().hex(), "png", save_slot=0)
                        previous = pixels
                    if page != last_page:
                        last_page = page
                        if auto:
                            deadline = time.monotonic() + args.page_seconds
                            LOGGER.info("Tela %s enviada.", page)
                    if args.once:
                        LOGGER.info("Quadro %s enviado. %s", page, sample)
                        return
                    await asyncio.sleep(max(0, min(started + args.interval, deadline) - time.monotonic()))
        except (BleakError, ConnectionError, TimeoutError, OSError, RuntimeError) as error:
            if args.once:
                raise
            LOGGER.warning("Conexão interrompida (%s). Nova tentativa em 5 s.", error)
            await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Monitor do Windows para painel iPixel Color 96x16.")
    parser.add_argument("--address", help="Endereço Bluetooth do painel (XX:XX:XX:XX:XX:XX).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--scan", action="store_true", help="Lista painéis próximos sem se conectar.")
    mode.add_argument("--once", action="store_true", help="Envia um quadro e encerra.")
    mode.add_argument("--preview", type=Path, help="Gera PNG ampliado sem conectar ao painel.")
    parser.add_argument("--scan-seconds", type=float, default=8, help="Duração da busca Bluetooth.")
    parser.add_argument("--interval", type=float, default=2, help="Intervalo de atualização em segundos.")
    parser.add_argument("--page-seconds", type=float, default=8, help="Tempo de exibição de cada tela.")
    parser.add_argument("--page", choices=("auto", "principal", "detalhes"), default="auto")
    parser.add_argument("--preview-page", choices=("principal", "detalhes"), default="principal")
    parser.add_argument("--disk", help="Unidade monitorada, por exemplo D:. Padrão: unidade do Windows.")
    parser.add_argument("--gpu-index", type=int, default=0, help="Índice da GPU NVIDIA no nvidia-smi.")
    cpu = parser.add_mutually_exclusive_group()
    cpu.add_argument("--dell-cpu-sensor", type=int, help="ID do sensor térmico da CPU no firmware Dell.")
    cpu.add_argument("--pawnio", action="store_true", help="Usa o PawnIO e os módulos preparados pelo projeto.")
    cpu.add_argument("--pawnio-modules", type=Path, help="Pasta com os módulos oficiais assinados do PawnIO.")
    lhm = parser.add_mutually_exclusive_group()
    lhm.add_argument("--lhm-url", default=DEFAULT_LHM_URL, help="URL JSON do LibreHardwareMonitor.")
    lhm.add_argument("--no-lhm", dest="lhm_url", action="store_const", const=None, help="Desativa a consulta.")
    parser.add_argument("--log-file", type=Path, help="Registra eventos em arquivo com rotação.")
    args = parser.parse_args()

    if sys.platform != "win32":
        parser.error("Este monitor requer Windows.")
    if not (args.scan or args.preview or args.address):
        parser.error("Informe --address ou use --scan para encontrar o painel.")
    if args.address and not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", args.address):
        parser.error("--address deve ter o formato XX:XX:XX:XX:XX:XX.")
    if args.disk and not re.fullmatch(r"[A-Za-z]:?", args.disk):
        parser.error("--disk deve ser uma letra de unidade, como D:.")
    if not 0.5 <= args.interval <= 60:
        parser.error("--interval deve estar entre 0.5 e 60 segundos.")
    if not 1 <= args.page_seconds <= 300:
        parser.error("--page-seconds deve estar entre 1 e 300 segundos.")
    if not 1 <= args.scan_seconds <= 60:
        parser.error("--scan-seconds deve estar entre 1 e 60 segundos.")
    if args.gpu_index < 0:
        parser.error("--gpu-index deve ser zero ou maior.")
    if args.dell_cpu_sensor is not None and not 1 <= args.dell_cpu_sensor <= 255:
        parser.error("--dell-cpu-sensor deve estar entre 1 e 255.")

    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(args.log_file, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(level=logging.WARNING, handlers=[handler], format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        LOGGER.info("Monitor encerrado.")
    except (BleakError, OSError, RuntimeError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
