import argparse
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile


INSTALLER_URL = "https://github.com/namazso/PawnIO.Setup/releases/download/2.2.0/PawnIO_setup.exe"
INSTALLER_HASH = "1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032"
MODULES_URL = "https://github.com/namazso/PawnIO.Modules/releases/download/0.2.12/release_0_2_12.zip"
MODULES_HASH = "09698897ce19b92f1043a2e322cfcf0b1fe823e0b576971b178a65244fb0af9d"
MODULE_HASHES = {
    "IntelMSR.bin": "30a5a302283de1e080dfee99e686eca91d7e53e911095cb91a320fb961f1ce12",
    "AMDFamily17.bin": "dae74615761b78bdf064dfb3e136252ddcc6fc727d88f14738d0e5800d427a91",
}


def has_hash(path, expected):
    if not path.is_file():
        return False
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest() == expected


def download_verified(url, path, expected):
    with urlopen(Request(url, headers={"User-Agent": "iPixel-Monitor"}), timeout=45) as response:
        with path.open("wb") as file:
            shutil.copyfileobj(response, file)
    if not has_hash(path, expected):
        raise ValueError(f"SHA-256 inválido em {path.name}.")


def verify_installer_signature(installer):
    directory = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory))
    if not 0 < length < len(directory):
        raise OSError("Não foi possível localizar o diretório do Windows.")
    powershell = Path(directory.value) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    security_module = r"Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1"
    command = (
        "$ErrorActionPreference = 'Stop'; "
        f"Import-Module (Join-Path $PSHOME '{security_module}'); "
        "if ((Get-AuthenticodeSignature -LiteralPath $env:IPIXEL_SETUP_PATH).Status -ne 'Valid') { exit 1 }; exit 0"
    )
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "PSModulePath": str(powershell.parent / "Modules"), "IPIXEL_SETUP_PATH": str(installer)},
        creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=60,
    )
    if result.returncode:
        raise ValueError("A assinatura Authenticode do instalador PawnIO não foi validada.")


def prepare(destination, driver_library, download_only):
    destination.mkdir(parents=True, exist_ok=True)
    missing_modules = [name for name, sha256 in MODULE_HASHES.items() if not has_hash(destination / name, sha256)]
    with tempfile.TemporaryDirectory(prefix=".preparar-", dir=destination) as staging_path:
        staging = Path(staging_path)
        if missing_modules:
            archive_path = staging / "modules.zip"
            download_verified(MODULES_URL, archive_path, MODULES_HASH)
            with ZipFile(archive_path) as archive:
                for name in missing_modules:
                    target = staging / name
                    with archive.open(name) as entry, target.open("wb") as file:
                        shutil.copyfileobj(entry, file)
                    if not has_hash(target, MODULE_HASHES[name]):
                        raise ValueError(f"SHA-256 inválido em {name}.")

        installer_name = "PawnIO_setup.exe"
        installer = staging / installer_name
        if download_only:
            if not has_hash(destination / installer_name, INSTALLER_HASH):
                download_verified(INSTALLER_URL, installer, INSTALLER_HASH)
                verify_installer_signature(installer)
                installer.replace(destination / installer_name)
        elif not driver_library.is_file():
            download_verified(INSTALLER_URL, installer, INSTALLER_HASH)
            verify_installer_signature(installer)
            print("Confirme a instalação na janela do PawnIO.", flush=True)
            subprocess.run([str(installer)], check=False)
            if not driver_library.is_file():
                raise OSError("A instalação do PawnIO não foi concluída.")

        for name in missing_modules:
            (staging / name).replace(destination / name)

    print(f"Arquivos oficiais verificados em: {destination}")


def main():
    parser = argparse.ArgumentParser(description="Prepara os módulos e o instalador oficial do PawnIO.")
    parser.add_argument("--download-to", type=Path, help="Salva os arquivos verificados sem instalar o driver.")
    args = parser.parse_args()
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        parser.error("Use Python de 64 bits no Windows.")
    if args.download_to is None and not ctypes.windll.shell32.IsUserAnAdmin():
        parser.error("Abra o terminal como administrador para preparar o PawnIO.")

    from pawnio_temperature import pawnio_paths

    try:
        driver_library, default_modules = pawnio_paths()
        destination = args.download_to.resolve() if args.download_to else default_modules
        prepare(destination, driver_library, download_only=args.download_to is not None)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
