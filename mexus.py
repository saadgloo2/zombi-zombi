"""
MEXUS Server Manager

Herramienta sencilla para descargar, configurar e iniciar servidores de
Minecraft: Paper, Fabric, Vanilla y Forge.

Uso rapido:
    python mexus.py
    python mexus.py start
    python mexus.py versions --type paper
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from types import NoneType
from typing import ClassVar, Iterable, TypeAlias, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree


APP_NAME = "MEXUS Server Manager"
APP_VERSION = "3.1.1"
CONFIG_FILE = "mexus_config.json"
SERVER_JAR = "server.jar"
FORGE_INSTALLER = "forge-installer.jar"
BACKUP_DIR = "backups"
DEFAULT_SERVER_PROPERTIES = "server.properties"
REQUEST_TIMEOUT = 30
SERVER_TYPES: tuple[str, ...] = ("paper", "fabric", "vanilla", "forge", "hybrid")
RAM_PATTERN = re.compile(r"^[1-9][0-9]*[MGmg]$")
MINECRAFT_RELEASE_PATTERN = re.compile(r"^1\.\d+(?:\.\d+)?$")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

ConfigValue: TypeAlias = str | bool
ConfigData: TypeAlias = dict[str, ConfigValue]
JSONValue: TypeAlias = (
    NoneType | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONObject: TypeAlias = dict[str, JSONValue]
JSONArray: TypeAlias = list[JSONValue]

if os.name == "nt":
    # Enables ANSI colors in most modern Windows terminals.
    os.system("")


class UI:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    COLOR_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    @classmethod
    def style(cls, text: str, *codes: str) -> str:
        if not cls.COLOR_ENABLED:
            return text
        return "".join(codes) + text + cls.RESET

    @classmethod
    def success(cls, message: str) -> None:
        print(cls.style("[OK] ", cls.GREEN, cls.BOLD) + message)

    @classmethod
    def error(cls, message: str) -> None:
        print(cls.style("[ERROR] ", cls.RED, cls.BOLD) + message)

    @classmethod
    def warning(cls, message: str) -> None:
        print(cls.style("[WARN] ", cls.YELLOW, cls.BOLD) + message)

    @classmethod
    def info(cls, message: str) -> None:
        print(cls.style("[INFO] ", cls.BLUE, cls.BOLD) + message)

    @classmethod
    def title(cls, message: str) -> None:
        print(cls.style(message, cls.CYAN, cls.BOLD))

    @classmethod
    def dim(cls, message: str) -> str:
        return cls.style(message, cls.DIM)

    @staticmethod
    def visible_len(text: str) -> int:
        return len(ANSI_PATTERN.sub("", text))

    @classmethod
    def clear(cls) -> None:
        os.system("cls" if os.name == "nt" else "clear")

    @classmethod
    def line(cls, width: int = 72) -> None:
        print(cls.style("-" * width, cls.DIM))

    @classmethod
    def box(cls, content: str | Iterable[str], width: int = 72) -> str:
        if isinstance(content, str):
            lines: list[str] = content.splitlines() or [""]
        else:
            lines = list(content)

        inner_width = max(width, *(cls.visible_len(line) for line in lines))
        top = "+" + "-" * (inner_width + 2) + "+"
        body: list[str] = []
        for line in lines:
            padding = inner_width - cls.visible_len(line)
            body.append(f"| {line}{' ' * padding} |")
        bottom = "+" + "-" * (inner_width + 2) + "+"
        return "\n".join([top, *body, bottom])

    @classmethod
    def banner(cls) -> str:
        title = cls.style("MEXUS", cls.CYAN, cls.BOLD)
        subtitle = cls.style("Minecraft Server Manager", cls.WHITE, cls.BOLD)
        return cls.box(
            [
                f"{title}  {subtitle}",
                f"Version {APP_VERSION} - Crear, configurar y publicar",
            ],
            width=58,
        )

    @classmethod
    def menu_item(cls, key: str, label: str, hint: str = "") -> None:
        key_text = cls.style(f"{key:>2}", cls.CYAN, cls.BOLD)
        if hint:
            print(f"  {key_text}  {label:<32} {cls.dim(hint)}")
        else:
            print(f"  {key_text}  {label}")

    @classmethod
    def section(cls, title: str) -> None:
        print(cls.style(title, cls.MAGENTA, cls.BOLD))


def normalize_ram(value: str) -> str:
    value = value.strip().upper()
    if not RAM_PATTERN.match(value):
        raise ValueError("Usa un valor como 2G, 4096M o 8G.")
    return value


def version_key(value: str) -> tuple[int, int, int, tuple[int, ...]]:
    minecraft_part, _, build_part = value.partition("-")
    parts = [int(part) for part in re.findall(r"\d+", minecraft_part)[:3]]
    while len(parts) < 3:
        parts.append(0)
    build_parts = tuple(int(part) for part in re.findall(r"\d+", build_part)[:4])
    return (parts[0], parts[1], parts[2], build_parts)


def is_minecraft_release(value: str) -> bool:
    return bool(MINECRAFT_RELEASE_PATTERN.match(value))


def yes(value: str) -> bool:
    return value.strip().lower() in {"s", "si", "y", "yes"}


def hybrid_server_recommendation(version: str) -> str:
    normalized = version.strip()
    if normalized == "1.20.1":
        return "Mohist (recomendado)"
    if normalized == "1.16.5":
        return "Arclight o Magma"
    return "Mohist (recomendado) o Arclight/Magma"


def expect_object(value: JSONValue, name: str) -> JSONObject:
    if isinstance(value, dict):
        return cast(JSONObject, value)
    raise RuntimeError(f"Respuesta invalida de {name}: se esperaba un objeto.")


def expect_list(value: JSONValue, name: str) -> JSONArray:
    if isinstance(value, list):
        return cast(JSONArray, value)
    raise RuntimeError(f"Respuesta invalida de {name}: se esperaba una lista.")


def network_error_message(url: str, exc: Exception) -> str:
    host = urlparse(url).netloc or url
    if isinstance(exc, HTTPError):
        return f"{host} respondio con HTTP {exc.code}. Intenta de nuevo mas tarde."
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, socket.gaierror):
            return (
                f"No se pudo resolver {host}. Revisa tu internet o DNS y vuelve a intentar."
            )
        return f"No se pudo conectar con {host}: {reason}"
    return f"No se pudo conectar con {host}: {exc}"


class Config:
    DEFAULTS: ClassVar[ConfigData] = {
        "type": "paper",
        "version": "",
        "ram_min": "2G",
        "ram_max": "4G",
        "java_path": "java",
        "auto_backup": True,
        "installed_type": "",
        "installed_version": "",
    }

    def __init__(self, path: Path | str = CONFIG_FILE) -> None:
        self.path = Path(path)
        self.data: ConfigData = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        self.data = dict(self.DEFAULTS)
        if not self.path.exists():
            return
        try:
            saved = cast(object, json.loads(self.path.read_text(encoding="utf-8")))
            if isinstance(saved, dict):
                saved_config = cast(dict[object, object], saved)
                for key, value in saved_config.items():
                    if isinstance(key, str) and isinstance(value, (str, bool)):
                        self.data[key] = value
        except json.JSONDecodeError:
            UI.warning(f"No se pudo leer {self.path}. Se usara configuracion nueva.")

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def get(self, key: str, default: ConfigValue | None = None) -> ConfigValue | None:
        return self.data.get(key, default)

    def get_str(self, key: str, default: str = "") -> str:
        value = self.data.get(key)
        return value if isinstance(value, str) else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.data.get(key)
        return value if isinstance(value, bool) else default

    def set(self, key: str, value: ConfigValue) -> None:
        self.data[key] = value
        self.save()

    def update(self, **values: ConfigValue) -> None:
        self.data.update(values)
        self.save()

    def is_ready(self) -> bool:
        return self.get_str("type") in SERVER_TYPES and bool(self.get_str("version").strip())

    def mark_installed(self) -> None:
        self.update(
            installed_type=self.get_str("type"),
            installed_version=self.get_str("version"),
        )

    def install_matches_config(self) -> bool:
        return (
            self.get_str("installed_type") == self.get_str("type")
            and self.get_str("installed_version") == self.get_str("version")
        )


class HttpClient:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {
            "User-Agent": (
                f"{APP_NAME}/{APP_VERSION} "
                "(https://github.com/mexus-server-manager/mexus)"
            )
        }

    def json(self, url: str) -> JSONValue:
        request = Request(url, headers=self.headers)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                payload = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(network_error_message(url, exc)) from exc

        return cast(JSONValue, json.loads(payload))

    def text(self, url: str) -> str:
        request = Request(url, headers=self.headers)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(network_error_message(url, exc)) from exc

    def download(self, url: str, target: Path) -> Path:
        target = Path(target)
        temp_target = target.with_suffix(target.suffix + ".part")
        if temp_target.exists():
            temp_target.unlink()

        request = Request(url, headers=self.headers)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                with temp_target.open("wb") as file:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        file.write(chunk)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            temp_target.unlink(missing_ok=True)
            raise RuntimeError(network_error_message(url, exc)) from exc

        if temp_target.stat().st_size == 0:
            temp_target.unlink(missing_ok=True)
            raise RuntimeError("La descarga llego vacia.")

        temp_target.replace(target)
        return target


class ServerDownloader:
    def __init__(self) -> None:
        self.http = HttpClient()

    def _get_fabric_loader_candidates(self, game_version: str) -> list[str]:
        candidate_urls = [
            f"https://meta.fabricmc.net/v2/versions/loader/{game_version}",
            "https://meta.fabricmc.net/v2/versions/loader",
        ]
        candidates: list[tuple[bool, tuple[int, int, int, tuple[int, ...]], str]] = []

        for url in candidate_urls:
            try:
                payload = expect_list(self.http.json(url), "loaders de Fabric")
            except RuntimeError:
                continue

            for item in payload:
                if not isinstance(item, dict):
                    continue

                loader_info = item.get("loader")
                if isinstance(loader_info, dict):
                    version_value = loader_info.get("version")
                    stable_value = loader_info.get("stable")
                else:
                    version_value = item.get("version")
                    stable_value = item.get("stable")

                if not isinstance(version_value, str):
                    continue

                stable = bool(stable_value) if isinstance(stable_value, bool) else False
                candidates.append((stable, version_key(version_value), version_value))

            if candidates:
                break

        if not candidates:
            raise RuntimeError("No se encontro una version valida de Fabric Loader.")

        stable_candidates = [candidate for candidate in candidates if candidate[0]]
        if stable_candidates:
            candidates = stable_candidates

        candidates.sort(key=lambda candidate: candidate[1], reverse=True)
        return [candidate[2] for candidate in candidates]

    def _get_fabric_installer_version(self) -> str:
        metadata = self.http.text(
            "https://maven.fabricmc.net/net/fabricmc/fabric-installer/maven-metadata.xml"
        )
        root = ElementTree.fromstring(metadata)
        versions = []
        for item in root.findall("./versioning/versions/version"):
            version_text = item.text
            if version_text:
                versions.append(version_text.strip())
        if not versions:
            raise RuntimeError("No se encontro una version valida de Fabric Installer.")
        return sorted(set(versions), key=version_key, reverse=True)[0]

    def install_fabric_server(self, version: str, root: Path, java_path: str = "java") -> Path:
        root = Path(root)
        loader_versions = self._get_fabric_loader_candidates(version)
        installer_version = self._get_fabric_installer_version()
        last_error: RuntimeError | None = None

        for loader_version in loader_versions:
            installer_name = f"fabric-installer-{installer_version}.jar"
            installer_path = root / installer_name
            installer_url = (
                "https://maven.fabricmc.net/net/fabricmc/fabric-installer/"
                f"{installer_version}/{installer_name}"
            )

            try:
                UI.info(
                    f"Instalando Fabric {version} con loader {loader_version}..."
                )
                self.http.download(installer_url, installer_path)
                completed = subprocess.run(
                    [
                        java_path,
                        "-jar",
                        str(installer_path),
                        "server",
                        "-dir",
                        str(root),
                        "-mcversion",
                        version,
                        "-loader",
                        loader_version,
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if completed.returncode == 0 and (root / SERVER_JAR).exists():
                    return root / SERVER_JAR
                if completed.returncode == 0:
                    UI.warning(
                        "El instalador de Fabric termino, pero no se encontro server.jar."
                    )
            except (RuntimeError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
                last_error = RuntimeError(str(exc))

        if last_error is not None:
            raise last_error
        raise RuntimeError("No se pudo instalar Fabric con ninguna version de loader.")

    def get_paper_versions(self) -> list[str]:
        data = expect_object(
            self.http.json("https://fill.papermc.io/v3/projects/paper"),
            "Paper",
        )
        version_groups = expect_object(data.get("versions"), "versiones de Paper")
        versions: list[str] = []
        for group_versions in version_groups.values():
            if not isinstance(group_versions, list):
                continue
            versions.extend(
                item
                for item in group_versions
                if isinstance(item, str) and is_minecraft_release(item)
            )
        return versions

    def get_paper_download_url(self, version: str) -> str:
        builds = expect_list(
            self.http.json(
                "https://fill.papermc.io/v3/projects/paper/"
                f"versions/{version}/builds"
            ),
            "builds de Paper",
        )

        stable_builds: list[JSONObject] = []
        fallback_builds: list[JSONObject] = []
        for item in builds:
            if not isinstance(item, dict):
                continue
            build = cast(JSONObject, item)
            fallback_builds.append(build)
            if build.get("channel") == "STABLE":
                stable_builds.append(build)

        candidates = stable_builds or fallback_builds
        if not candidates:
            raise RuntimeError(f"No hay builds de Paper para {version}.")

        if not stable_builds:
            UI.warning(
                f"No se encontro build STABLE de Paper {version}; "
                "se usara el build disponible mas reciente."
            )

        download = expect_object(
            expect_object(candidates[0].get("downloads"), "descargas de Paper").get(
                "server:default"
            ),
            "server.jar de Paper",
        )
        url = download.get("url")
        if not isinstance(url, str):
            raise RuntimeError(f"Paper {version} no tiene URL de descarga.")
        return url

    def normalize_minecraft_version(self, version: str) -> str:
        candidate = version.strip()
        if not candidate:
            return candidate
        return candidate.split("-", 1)[0]

    def resolve_forge_version(self, minecraft_version: str) -> str:
        version = self.normalize_minecraft_version(minecraft_version)
        if not version:
            raise RuntimeError("No se indico una version de Minecraft para Forge.")
        if "-" in minecraft_version and is_minecraft_release(self.normalize_minecraft_version(minecraft_version)):
            return minecraft_version.strip()

        text = self.http.text(
            "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
        )
        root = ElementTree.fromstring(text)
        candidates: list[str] = []
        for item in root.findall("./versioning/versions/version"):
            version_text = (item.text or "").strip()
            if not version_text:
                continue
            if version_text.startswith(version + "-"):
                candidates.append(version_text)

        if not candidates:
            raise RuntimeError(f"No existe una build de Forge para {version}.")

        return sorted(set(candidates), key=version_key, reverse=True)[0]

    def get_versions(self, server_type: str) -> list[str]:
        server_type = server_type.lower()
        versions: list[str]
        if server_type == "paper":
            versions = self.get_paper_versions()

        elif server_type == "fabric":
            data = expect_list(
                self.http.json("https://meta.fabricmc.net/v2/versions/game"),
                "versiones de Fabric",
            )
            versions = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                version = item.get("version")
                if item.get("stable") is True and isinstance(version, str):
                    versions.append(version)

        elif server_type == "vanilla":
            data = expect_object(
                self.http.json(
                    "https://piston-meta.mojang.com/mc/game/version_manifest.json"
                ),
                "manifest de Vanilla",
            )
            version_items = expect_list(data.get("versions"), "versiones de Vanilla")
            versions = []
            for item in version_items:
                if not isinstance(item, dict):
                    continue
                version = item.get("id")
                if item.get("type") == "release" and isinstance(version, str):
                    versions.append(version)

        elif server_type == "forge":
            text = self.http.text(
                "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
            )
            root = ElementTree.fromstring(text)
            versions = []
            for item in root.findall("./versioning/versions/version"):
                version_text = (item.text or "").strip()
                if version_text and "-" in version_text:
                    versions.append(version_text)

        elif server_type == "hybrid":
            data = expect_object(
                self.http.json(
                    "https://piston-meta.mojang.com/mc/game/version_manifest.json"
                ),
                "manifest de versiones de Minecraft",
            )
            version_items = expect_list(data.get("versions"), "versiones de Minecraft")
            versions = []
            for item in version_items:
                if not isinstance(item, dict):
                    continue
                version = item.get("id")
                if item.get("type") == "release" and isinstance(version, str):
                    versions.append(version)

        else:
            raise ValueError(f"Tipo de servidor no soportado: {server_type}")

        return sorted(set(versions), key=version_key, reverse=True)

    def download(self, server_type: str, version: str, root: Path) -> Path:
        server_type = server_type.lower()
        root = Path(root)

        if server_type == "paper":
            url = self.get_paper_download_url(version)
            target = root / SERVER_JAR

        elif server_type == "fabric":
            return self.install_fabric_server(version, root)

        elif server_type == "vanilla":
            manifest = expect_object(
                self.http.json(
                    "https://piston-meta.mojang.com/mc/game/version_manifest.json"
                ),
                "manifest de Vanilla",
            )
            version_items = expect_list(manifest.get("versions"), "versiones de Vanilla")
            version_info: JSONObject | None = None
            for item in version_items:
                if isinstance(item, dict) and item.get("id") == version:
                    version_info = item
                    break
            if not version_info:
                raise RuntimeError(f"No se encontro Vanilla {version}.")
            version_url = version_info.get("url")
            if not isinstance(version_url, str):
                raise RuntimeError(f"Vanilla {version} no tiene URL de metadata.")

            package = expect_object(self.http.json(version_url), f"Vanilla {version}")
            downloads = expect_object(package.get("downloads"), "descargas de Vanilla")
            server_download = expect_object(downloads.get("server"), "server.jar Vanilla")
            url_value = server_download.get("url")
            if not isinstance(url_value, str):
                raise RuntimeError(f"Vanilla {version} no tiene server.jar.")
            url = url_value
            target = root / SERVER_JAR

        elif server_type == "forge":
            forge_version = self.resolve_forge_version(version)
            url = (
                "https://maven.minecraftforge.net/net/minecraftforge/forge/"
                f"{forge_version}/forge-{forge_version}-installer.jar"
            )
            target = root / FORGE_INSTALLER

        elif server_type == "hybrid":
            recommendation = hybrid_server_recommendation(version)
            UI.info(
                f"Para mods y plugins en {version}, usa {recommendation}."
            )
            raise RuntimeError(
                "Los servidores híbridos como Mohist, Arclight o Magma no se descargan automáticamente desde MEXUS. "
                "Descarga el servidor manualmente y colócalo como server.jar."
            )

        else:
            raise ValueError(f"Tipo de servidor no soportado: {server_type}")

        UI.info(f"Descargando {server_type} {version}...")
        return self.http.download(url, target)


class ServerManager:
    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()
        self.config = Config(self.root / CONFIG_FILE)
        self.process: subprocess.Popen[str] | None = None

    @property
    def server_jar(self) -> Path:
        return self.root / SERVER_JAR

    @property
    def forge_installer(self) -> Path:
        return self.root / FORGE_INSTALLER

    def check_java(self) -> bool:
        java_path = self.config.get_str("java_path", "java")
        if shutil.which(java_path) is None and not Path(java_path).exists():
            UI.error("Java no esta instalado o no esta en el PATH.")
            print("Instala Java 21 para Minecraft moderno: https://adoptium.net/")
            return False

        try:
            completed = subprocess.run(
                [java_path, "-version"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            UI.error(f"No se pudo ejecutar Java: {exc}")
            return False

        output = (completed.stderr or completed.stdout).splitlines()
        if output:
            UI.info(output[0])
        return completed.returncode == 0

    def ensure_eula(self) -> bool:
        eula_path = self.root / "eula.txt"
        if eula_path.exists() and "eula=true" in eula_path.read_text(
            encoding="utf-8", errors="ignore"
        ).lower():
            return True

        UI.warning("Para iniciar el servidor debes aceptar la EULA de Minecraft.")
        print("Lee: https://aka.ms/MinecraftEULA")
        answer = input("Aceptas la EULA? (s/N): ")
        if not yes(answer):
            UI.error("No se inicio el servidor porque la EULA no fue aceptada.")
            return False

        eula_path.write_text("eula=true\n", encoding="utf-8")
        UI.success("EULA aceptada y guardada en eula.txt.")
        return True

    def write_forge_jvm_args(self) -> None:
        args = [
            f"-Xms{self.config.get_str('ram_min')}",
            f"-Xmx{self.config.get_str('ram_max')}",
            "-XX:+UseG1GC",
        ]
        (self.root / "user_jvm_args.txt").write_text("\n".join(args) + "\n", encoding="utf-8")

    def install_forge(self) -> bool:
        java_path = self.config.get_str("java_path", "java")
        version = self.config.get_str("version")

        if not self.forge_installer.exists() or not self.config.install_matches_config():
            ServerDownloader().download("forge", version, self.root)

        UI.info("Instalando Forge. Esto puede tardar unos minutos...")
        completed = subprocess.run(
            [java_path, "-jar", str(self.forge_installer), "--installServer"],
            cwd=self.root,
            text=True,
        )
        if completed.returncode != 0:
            UI.error("Forge no se pudo instalar.")
            return False

        self.write_forge_jvm_args()
        self.config.mark_installed()
        UI.success("Forge instalado correctamente.")
        return True

    def ensure_server_files(self) -> bool:
        self.config.load()
        server_type = self.config.get_str("type")
        version = self.config.get_str("version")

        if not self.config.is_ready():
            UI.error("Falta configurar tipo y version del servidor.")
            return False

        try:
            if server_type == "forge":
                run_script_exists = (self.root / "run.bat").exists() or (
                    self.root / "run.sh"
                ).exists()
                if not run_script_exists or not self.config.install_matches_config():
                    return self.install_forge()
                self.write_forge_jvm_args()
                return True

            if not self.server_jar.exists() or not self.config.install_matches_config():
                ServerDownloader().download(server_type, version, self.root)
                self.config.mark_installed()
            return True

        except Exception as exc:
            UI.error(f"No se pudo preparar el servidor: {exc}")
            return False

    def build_command(self) -> list[str]:
        server_type = self.config.get_str("type")
        java_path = self.config.get_str("java_path", "java")

        if server_type == "forge":
            self.write_forge_jvm_args()
            if os.name == "nt" and (self.root / "run.bat").exists():
                return ["cmd.exe", "/c", "run.bat", "nogui"]
            if (self.root / "run.sh").exists():
                return ["sh", "run.sh", "nogui"]

            version = self.config.get_str("version")
            forge_version = ServerDownloader().resolve_forge_version(version)
            args_name = "win_args.txt" if os.name == "nt" else "unix_args.txt"
            arg_file = (
                self.root
                / "libraries"
                / "net"
                / "minecraftforge"
                / "forge"
                / forge_version
                / args_name
            )
            if arg_file.exists():
                relative_arg_file = arg_file.relative_to(self.root).as_posix()
                return [java_path, "@user_jvm_args.txt", f"@{relative_arg_file}", "nogui"]

            raise RuntimeError("No se encontro run.bat/run.sh ni los args de Forge.")

        return [
            java_path,
            f"-Xms{self.config.get_str('ram_min')}",
            f"-Xmx{self.config.get_str('ram_max')}",
            "-XX:+UseG1GC",
            "-jar",
            SERVER_JAR,
            "nogui",
        ]

    def create_backup(self, automatic: bool = False) -> Path | None:
        items = [
            "world",
            "world_nether",
            "world_the_end",
            "server.properties",
            "whitelist.json",
            "ops.json",
            "banned-ips.json",
            "banned-players.json",
        ]
        existing = [self.root / item for item in items if (self.root / item).exists()]
        if not existing:
            if not automatic:
                UI.info("No hay mundos o archivos de servidor para respaldar todavia.")
            return None

        backups = self.root / BACKUP_DIR
        backups.mkdir(exist_ok=True)
        prefix = "auto" if automatic else "manual"
        backup_path = backups / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.zip"

        UI.info(f"Creando backup: {backup_path.name}")
        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in existing:
                if item.is_dir():
                    for child in item.rglob("*"):
                        if child.is_file():
                            archive.write(child, child.relative_to(self.root))
                else:
                    archive.write(item, item.relative_to(self.root))

        UI.success(f"Backup guardado en {backup_path}")
        return backup_path

    def start(self) -> bool:
        self.config.load()
        if not self.config.is_ready():
            UI.error("Primero configura el tipo y la version del servidor.")
            return False

        if not self.check_java():
            return False
        if not self.ensure_eula():
            return False
        if self.config.get_bool("auto_backup", True):
            self.create_backup(automatic=True)
        if not self.ensure_server_files():
            return False

        command = self.build_command()
        UI.success(
            f"Iniciando {self.config.get_str('type')} {self.config.get_str('version')} "
            f"con {self.config.get_str('ram_min')}-{self.config.get_str('ram_max')} RAM."
        )
        UI.info("Escribe comandos del servidor aqui. Usa 'stop' para apagar.")

        stop_sent = False
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.root,
                stdin=subprocess.PIPE,
                text=True,
            )

            while self.process.poll() is None:
                try:
                    command_text = input()
                except EOFError:
                    break

                if self.process.stdin is not None:
                    self.process.stdin.write(command_text + "\n")
                    self.process.stdin.flush()

                if command_text.strip().lower() == "stop":
                    stop_sent = True
                    break

        except KeyboardInterrupt:
            UI.warning("Interrupcion detectada. Deteniendo servidor...")
        except OSError as exc:
            UI.error(f"No se pudo iniciar el servidor: {exc}")
            return False
        finally:
            self.stop_gracefully(send_stop=not stop_sent)

        return self.process is not None and self.process.returncode == 0

    def stop_gracefully(self, send_stop: bool = True) -> None:
        if self.process is None or self.process.poll() is not None:
            return

        if send_stop and self.process.stdin is not None:
            try:
                self.process.stdin.write("stop\n")
                self.process.stdin.flush()
            except OSError:
                pass

        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            UI.warning("El servidor no se detuvo a tiempo. Cerrando proceso...")
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def print_status(config: Config) -> None:
    ready = "Configurado" if config.is_ready() else "Falta configurar"
    lines = [
        f"Estado: {ready}",
        f"Servidor: {config.get_str('type')} {config.get_str('version') or '-'}",
        f"RAM: {config.get_str('ram_min')} - {config.get_str('ram_max')}",
        f"Java: {config.get_str('java_path')}",
        f"Auto backup: {'si' if config.get_bool('auto_backup') else 'no'}",
    ]
    print(UI.box(lines, width=58))


def read_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_properties(path: Path, updates: dict[str, str]) -> None:
    current = read_properties(path)
    current.update(updates)

    preferred_order = [
        "server-name",
        "motd",
        "server-port",
        "max-players",
        "difficulty",
        "gamemode",
        "pvp",
        "online-mode",
        "white-list",
        "spawn-protection",
        "view-distance",
        "simulation-distance",
        "enable-command-block",
        "allow-flight",
    ]
    ordered_keys = [key for key in preferred_order if key in current]
    ordered_keys.extend(sorted(key for key in current if key not in ordered_keys))

    lines = [
        "# Minecraft server properties generated by MEXUS",
        f"# Updated {datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    lines.extend(f"{key}={current[key]}" for key in ordered_keys)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def configure_server_properties(manager: ServerManager) -> None:
    path = manager.root / DEFAULT_SERVER_PROPERTIES
    current = read_properties(path)

    UI.title("Configuracion rapida del servidor")
    print("Deja un campo vacio para conservar el valor actual.")
    print()

    defaults = {
        "motd": current.get("motd", "Servidor MEXUS"),
        "server-port": current.get("server-port", "25565"),
        "max-players": current.get("max-players", "20"),
        "difficulty": current.get("difficulty", "normal"),
        "gamemode": current.get("gamemode", "survival"),
        "pvp": current.get("pvp", "true"),
        "online-mode": current.get("online-mode", "true"),
        "white-list": current.get("white-list", "false"),
        "view-distance": current.get("view-distance", "10"),
        "simulation-distance": current.get("simulation-distance", "10"),
        "enable-command-block": current.get("enable-command-block", "false"),
    }

    updates: dict[str, str] = {}
    prompts = [
        ("motd", "Nombre/MOTD"),
        ("server-port", "Puerto"),
        ("max-players", "Jugadores maximos"),
        ("difficulty", "Dificultad (peaceful/easy/normal/hard)"),
        ("gamemode", "Modo (survival/creative/adventure/spectator)"),
        ("pvp", "PVP (true/false)"),
        ("online-mode", "Online mode premium (true/false)"),
        ("white-list", "Whitelist (true/false)"),
        ("view-distance", "View distance"),
        ("simulation-distance", "Simulation distance"),
        ("enable-command-block", "Command blocks (true/false)"),
    ]

    for key, label in prompts:
        value = input(f"{label} [{defaults[key]}]: ").strip()
        updates[key] = value or defaults[key]

    write_properties(path, updates)
    UI.success(f"Guardado: {path.name}")


def make_start_script(config: Config, windows: bool) -> str:
    java_path = config.get_str("java_path", "java")
    ram_min = config.get_str("ram_min", "2G")
    ram_max = config.get_str("ram_max", "4G")
    if windows:
        return (
            "@echo off\n"
            "title MEXUS Minecraft Server\n"
            f'"{java_path}" -Xms{ram_min} -Xmx{ram_max} -XX:+UseG1GC -jar {SERVER_JAR} nogui\n'
            "pause\n"
        )
    return (
        "#!/usr/bin/env sh\n"
        f'exec "{java_path}" -Xms{ram_min} -Xmx{ram_max} -XX:+UseG1GC -jar {SERVER_JAR} nogui\n'
    )


def generate_github_files(manager: ServerManager) -> None:
    config = manager.config
    server_type = config.get_str("type", "paper")
    version = config.get_str("version") or "sin configurar"

    files: dict[str, str] = {
        ".gitignore": "\n".join(
            [
                "# Server runtime",
                "world/",
                "world_nether/",
                "world_the_end/",
                "logs/",
                "crash-reports/",
                "backups/",
                "*.jar",
                "*.part",
                "libraries/",
                "versions/",
                "cache/",
                "eula.txt",
                "ops.json",
                "whitelist.json",
                "banned-ips.json",
                "banned-players.json",
                "",
            ]
        ),
        "README.md": "\n".join(
            [
                "# Minecraft Server",
                "",
                "Servidor generado con MEXUS Server Manager.",
                "",
                "## Configuracion",
                "",
                f"- Tipo: `{server_type}`",
                f"- Version: `{version}`",
                f"- RAM: `{config.get_str('ram_min')}` a `{config.get_str('ram_max')}`",
                f"- Java: `{config.get_str('java_path')}`",
                "",
                "## Recomendacion para mods + plugins",
                "",
                "Para combinar mods y plugins, lo mejor suele ser Mohist en 1.20.1. "
                "Alternativas viables son Arclight o Magma, especialmente en 1.16.5.",
                "",
                "Estilo visual sugerido: fotografía realista, grain, cinematic lighting, imperfect details, shot on camera.",
                "",
                "## Uso",
                "",
                "1. Instala Java 21.",
                "2. Ejecuta `python mexus.py` para configurar o iniciar el servidor.",
                "3. Acepta la EULA solo si estas de acuerdo con los terminos de Mojang.",
                "",
                "Los mundos, backups, logs y archivos pesados no se suben a GitHub.",
                "",
            ]
        ),
        "start-server.bat": make_start_script(config, windows=True),
        "start-server.sh": make_start_script(config, windows=False),
    }

    created: list[str] = []
    for name, content in files.items():
        path = manager.root / name
        if path.exists():
            answer = input(f"{name} ya existe. Sobrescribir? (s/N): ")
            if not yes(answer):
                continue
        path.write_text(content, encoding="utf-8")
        created.append(name)

    if created:
        UI.success("Archivos listos para GitHub: " + ", ".join(created))
    else:
        UI.info("No se cambio ningun archivo.")


def pause() -> None:
    input("\nPresiona Enter para continuar...")


def choose_server_type(config: Config) -> None:
    UI.line()
    print("Tipos disponibles:")
    for index, server_type in enumerate(SERVER_TYPES, start=1):
        print(f"  {index}. {server_type}")
    print("\nRecomendacion para mods + plugins: hybrid -> Mohist 1.20.1")
    UI.line()

    current_type = config.get_str("type", "paper")
    raw_type = input(f"Tipo [{current_type}]: ").strip().lower()
    if raw_type.isdigit():
        selected_index = int(raw_type) - 1
        if 0 <= selected_index < len(SERVER_TYPES):
            server_type = SERVER_TYPES[selected_index]
        else:
            UI.error("Numero invalido.")
            return
    else:
        server_type = raw_type or current_type

    if server_type not in SERVER_TYPES:
        UI.error("Tipo invalido. Usa: " + ", ".join(SERVER_TYPES))
        return

    downloader = ServerDownloader()
    versions: list[str] = []
    try:
        versions = downloader.get_versions(server_type)
    except Exception as exc:
        UI.warning(str(exc))
        UI.info(
            "Puedes escribir la version manualmente, pero para descargar el servidor "
            "necesitaras internet funcionando."
        )

    if versions:
        print("\nVersiones recientes:")
        for index, version in enumerate(versions[:25], start=1):
            print(f"  {index:2}. {version}")
        print("\nPuedes escribir un numero de la lista o una version exacta.")
    else:
        print("\nEscribe una version exacta, por ejemplo 1.21.4, 1.20.1 o 1.19.4.")

    raw_version = input("Version: ").strip()
    if not raw_version:
        UI.error("La version no puede estar vacia.")
        return

    if raw_version.isdigit() and versions:
        selected_index = int(raw_version) - 1
        if not 0 <= selected_index < len(versions):
            UI.error("Numero de version invalido.")
            return
        version = versions[selected_index]
    else:
        version = raw_version

    config.update(
        type=server_type,
        version=version,
        installed_type="",
        installed_version="",
    )
    UI.success(f"Configurado: {server_type} {version}")


def configure_ram(config: Config) -> None:
    UI.line()
    print("Presets:")
    print("  1. 2G - 4G  (servidor pequeno)")
    print("  2. 4G - 8G  (servidor mediano)")
    print("  3. 6G - 12G (mods o varios jugadores)")
    print("  4. 8G - 16G (servidor pesado)")
    print("  5. Personalizado")
    UI.line()
    choice = input("Opcion: ").strip()

    try:
        if choice == "1":
            ram_min, ram_max = "2G", "4G"
        elif choice == "2":
            ram_min, ram_max = "4G", "8G"
        elif choice == "3":
            ram_min, ram_max = "6G", "12G"
        elif choice == "4":
            ram_min, ram_max = "8G", "16G"
        elif choice == "5":
            ram_min = normalize_ram(input("RAM minima (ej. 2G): "))
            ram_max = normalize_ram(input("RAM maxima (ej. 4G): "))
        else:
            UI.error("Opcion invalida.")
            return
    except ValueError as exc:
        UI.error(str(exc))
        return

    config.update(ram_min=ram_min, ram_max=ram_max)
    UI.success(f"RAM configurada: {ram_min} - {ram_max}")


def configure_java(config: Config) -> None:
    current = config.get_str("java_path", "java")
    value = input(f"Ruta de Java [{current}]: ").strip() or current
    config.set("java_path", value)
    UI.success(f"Java configurado: {value}")


def toggle_backup(config: Config) -> None:
    new_value = not config.get_bool("auto_backup", True)
    config.set("auto_backup", new_value)
    UI.success(f"Auto backup: {'activado' if new_value else 'desactivado'}")


def doctor(manager: ServerManager) -> None:
    UI.title("Revision del entorno")
    print(f"Carpeta: {manager.root}")
    print(f"Config: {manager.config.path}")
    manager.check_java()
    UI.success("Dependencias Python externas: ninguna.")
    if manager.config.is_ready():
        UI.success("Configuracion lista.")
    else:
        UI.warning("Configura tipo y version antes de iniciar.")


def interactive_menu() -> None:
    manager = ServerManager()
    config = manager.config

    while True:
        config.load()
        UI.clear()
        print(UI.banner())
        print()
        print_status(config)
        print()
        UI.section("Servidor")
        UI.menu_item("1", "Iniciar servidor", "descarga si hace falta")
        UI.menu_item("2", "Cambiar tipo/version", "Paper, Fabric, Vanilla, Forge, Hybrid")
        UI.menu_item("3", "Configurar RAM", "presets o manual")
        UI.menu_item("4", "Editar server.properties", "MOTD, puerto, modo, whitelist")
        print()
        UI.section("Herramientas")
        UI.menu_item("5", "Crear backup manual", "mundo y archivos importantes")
        UI.menu_item("6", "Preparar archivos GitHub", "README, .gitignore, start scripts")
        UI.menu_item("7", "Cambiar ruta de Java")
        UI.menu_item("8", "Activar/desactivar auto backup")
        UI.menu_item("9", "Revisar entorno")
        print()
        UI.menu_item("0", "Salir")
        print()

        choice = input("Selecciona una opcion: ").strip()
        print()

        if choice == "1":
            manager.start()
            pause()
        elif choice == "2":
            choose_server_type(config)
            pause()
        elif choice == "3":
            configure_ram(config)
            pause()
        elif choice == "4":
            configure_server_properties(manager)
            pause()
        elif choice == "5":
            manager.create_backup(automatic=False)
            pause()
        elif choice == "6":
            generate_github_files(manager)
            pause()
        elif choice == "7":
            configure_java(config)
            pause()
        elif choice == "8":
            toggle_backup(config)
            pause()
        elif choice == "9":
            doctor(manager)
            pause()
        elif choice == "0":
            UI.success("Hasta luego.")
            break
        else:
            UI.error("Opcion invalida.")
            time.sleep(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mexus.py",
        description="Descarga, configura e inicia servidores de Minecraft.",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("start", help="Inicia el servidor configurado.")
    subparsers.add_parser("backup", help="Crea un backup manual.")
    subparsers.add_parser("doctor", help="Revisa Java, dependencias y configuracion.")
    subparsers.add_parser(
        "properties",
        help="Abre el asistente para crear o editar server.properties.",
    )
    subparsers.add_parser(
        "github-files",
        help="Genera README, .gitignore y scripts para subir el proyecto a GitHub.",
    )

    versions_parser = subparsers.add_parser("versions", help="Muestra versiones disponibles.")
    versions_parser.add_argument("--type", choices=SERVER_TYPES, default="paper")
    versions_parser.add_argument("--limit", type=int, default=25)

    config_parser = subparsers.add_parser("config", help="Actualiza la configuracion.")
    config_parser.add_argument("--type", choices=SERVER_TYPES)
    config_parser.add_argument("--version")
    config_parser.add_argument("--ram-min")
    config_parser.add_argument("--ram-max")
    config_parser.add_argument("--java")
    config_parser.add_argument("--auto-backup", choices=("on", "off"))

    return parser


def run_cli(args: argparse.Namespace) -> int:
    manager = ServerManager()
    config = manager.config
    command = cast(str | None, getattr(args, "command", None))

    if command == "start":
        return 0 if manager.start() else 1

    if command == "backup":
        manager.create_backup(automatic=False)
        return 0

    if command == "doctor":
        doctor(manager)
        return 0

    if command == "properties":
        configure_server_properties(manager)
        return 0

    if command == "github-files":
        generate_github_files(manager)
        return 0

    if command == "versions":
        downloader = ServerDownloader()
        server_type = cast(str, getattr(args, "type", "paper"))
        limit = cast(int, getattr(args, "limit", 25))
        versions = downloader.get_versions(server_type)
        for version in versions[: max(limit, 1)]:
            print(version)
        return 0

    if command == "config":
        updates: ConfigData = {}
        server_type = cast(str | None, getattr(args, "type", None))
        version = cast(str | None, getattr(args, "version", None))
        ram_min = cast(str | None, getattr(args, "ram_min", None))
        ram_max = cast(str | None, getattr(args, "ram_max", None))
        java_path = cast(str | None, getattr(args, "java", None))
        auto_backup = cast(str | None, getattr(args, "auto_backup", None))

        if server_type:
            updates["type"] = server_type
            updates["installed_type"] = ""
            updates["installed_version"] = ""
        if version:
            updates["version"] = version
            updates["installed_type"] = ""
            updates["installed_version"] = ""
        if ram_min:
            updates["ram_min"] = normalize_ram(ram_min)
        if ram_max:
            updates["ram_max"] = normalize_ram(ram_max)
        if java_path:
            updates["java_path"] = java_path
        if auto_backup:
            updates["auto_backup"] = auto_backup == "on"

        if not updates:
            print_status(config)
            return 0

        config.update(**updates)
        UI.success("Configuracion actualizada.")
        print_status(config)
        return 0

    interactive_menu()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_cli(args)
    except KeyboardInterrupt:
        UI.warning("Operacion cancelada.")
        return 130
    except Exception as exc:
        UI.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
