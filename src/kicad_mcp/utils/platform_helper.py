"""Cross-platform KiCad installation detection."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from kicad_mcp.logging_config import get_logger

logger = get_logger("platform")


def get_platform() -> str:
    """Return the current platform identifier."""
    if sys.platform == "win32":
        return "windows"
    elif sys.platform == "darwin":
        return "macos"
    return "linux"


def find_kicad_cli() -> Path | None:
    """Find the kicad-cli executable on this system.

    Checks PATH first, then platform-specific default install locations.

    Returns:
        Path to kicad-cli if found, None otherwise.
    """
    # Check PATH first
    cli_in_path = shutil.which("kicad-cli")
    if cli_in_path:
        logger.debug("Found kicad-cli in PATH: %s", cli_in_path)
        return Path(cli_in_path)

    platform = get_platform()
    candidates: list[Path] = []

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for base in [program_files, program_files_x86]:
            for version in ["9.0", "8.0", "7.0"]:
                candidates.append(Path(base) / "KiCad" / version / "bin" / "kicad-cli.exe")

    elif platform == "macos":
        candidates.extend([
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
            Path("/Applications/KiCad/kicad-cli"),
        ])

    else:  # linux
        candidates.extend([
            Path("/usr/bin/kicad-cli"),
            Path("/usr/local/bin/kicad-cli"),
            Path("/snap/kicad/current/usr/bin/kicad-cli"),
        ])

    for candidate in candidates:
        if candidate.exists():
            logger.debug("Found kicad-cli at: %s", candidate)
            return candidate

    logger.debug("kicad-cli not found on this system")
    return None


def find_kicad_python_paths() -> list[Path]:
    """Find KiCad's Python module paths for SWIG bindings (pcbnew).

    Returns:
        List of existing paths that may contain pcbnew module.
    """
    paths: list[Path] = []
    platform = get_platform()

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        for version in ["9.0", "8.0", "7.0"]:
            p = Path(program_files) / "KiCad" / version / "lib" / "python3" / "dist-packages"
            if p.exists():
                paths.append(p)

    elif platform == "macos":
        base = Path("/Applications/KiCad/KiCad.app/Contents/Frameworks/"
                     "Python.framework/Versions")
        if base.exists():
            for pyver in sorted(base.iterdir(), reverse=True):
                sp = pyver / "lib" / f"python{pyver.name}" / "site-packages"
                if sp.exists():
                    paths.append(sp)

    else:  # linux
        linux_paths = [
            Path("/usr/lib/kicad/lib/python3/dist-packages"),
            Path("/usr/lib/python3/dist-packages"),
            Path("/usr/share/kicad/scripting/plugins"),
        ]
        for p in linux_paths:
            if p.exists():
                paths.append(p)

    return paths


def add_kicad_to_sys_path() -> bool:
    """Add KiCad Python paths to sys.path if not already present.

    Returns:
        True if any paths were added.
    """
    added = False
    for p in find_kicad_python_paths():
        p_str = str(p)
        if p_str not in sys.path:
            sys.path.insert(0, p_str)
            logger.debug("Added to sys.path: %s", p_str)
            added = True
    return added


def detect_kicad_version() -> str | None:
    """Try to detect the installed KiCad version.

    Returns:
        Version string like '9.0.1' or None if not detectable.
    """
    import subprocess

    cli = find_kicad_cli()
    if cli:
        try:
            result = subprocess.run(
                [str(cli), "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                logger.debug("KiCad version from CLI: %s", version)
                return version
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("Failed to get KiCad version: %s", e)

    return None


def find_java() -> Path | None:
    """Find a Java executable on this system.

    Checks PATH first, then JAVA_HOME, then platform-specific default locations.

    Returns:
        Path to java executable if found, None otherwise.
    """
    # Check PATH first
    java_in_path = shutil.which("java")
    if java_in_path:
        logger.debug("Found java in PATH: %s", java_in_path)
        return Path(java_in_path)

    # Check JAVA_HOME
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        java_bin = Path(java_home) / "bin" / ("java.exe" if get_platform() == "windows" else "java")
        if java_bin.exists():
            logger.debug("Found java via JAVA_HOME: %s", java_bin)
            return java_bin

    platform = get_platform()
    candidates: list[Path] = []

    if platform == "windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        # Eclipse Adoptium / Temurin
        adoptium_base = Path(program_files) / "Eclipse Adoptium"
        if adoptium_base.exists():
            for jre_dir in sorted(adoptium_base.iterdir(), reverse=True):
                candidates.append(jre_dir / "bin" / "java.exe")
        # Oracle / OpenJDK
        java_base = Path(program_files) / "Java"
        if java_base.exists():
            for jdk_dir in sorted(java_base.iterdir(), reverse=True):
                candidates.append(jdk_dir / "bin" / "java.exe")

    elif platform == "macos":
        candidates.extend([
            Path("/usr/bin/java"),
            Path("/Library/Java/JavaVirtualMachines"),
        ])
        # Homebrew
        for brew_dir in [Path("/opt/homebrew/opt/openjdk/bin/java"),
                         Path("/usr/local/opt/openjdk/bin/java")]:
            candidates.append(brew_dir)

    else:  # linux
        candidates.extend([
            Path("/usr/bin/java"),
            Path("/usr/local/bin/java"),
        ])

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            logger.debug("Found java at: %s", candidate)
            return candidate

    logger.debug("java not found on this system")
    return None


def find_freerouting_jar() -> Path | None:
    """Find a FreeRouting JAR file on this system.

    Checks ~/.kicad-mcp/freerouting/ first, then common KiCad plugin directories.

    Returns:
        Path to freerouting JAR if found, None otherwise.
    """
    search_dirs: list[Path] = []

    # User data directory
    if os.name == "nt":
        base = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        base = Path.home()
    search_dirs.append(base / ".kicad-mcp" / "freerouting")

    # KiCad plugin directories
    platform = get_platform()
    if platform == "windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            for version in ["9.0", "8.0", "7.0"]:
                search_dirs.append(
                    Path(appdata) / "kicad" / version / "3rdparty" / "plugins"
                    / "com_github_freerouting_freerouting" / "plugins" / "jar"
                )
        documents = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
        for version in ["9.0", "8.0", "7.0"]:
            search_dirs.append(
                documents / "KiCad" / version / "3rdparty" / "plugins"
                / "com_github_freerouting_freerouting" / "plugins" / "jar"
            )
    elif platform == "macos":
        search_dirs.append(
            Path.home() / "Library" / "Preferences" / "kicad" / "scripting" / "plugins"
        )
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        search_dirs.append(config_home / "kicad" / "scripting" / "plugins")

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        # Find JAR files matching freerouting pattern
        jars = sorted(search_dir.glob("freerouting*.jar"), reverse=True)
        if jars:
            logger.debug("Found FreeRouting JAR: %s", jars[0])
            return jars[0]

    logger.debug("FreeRouting JAR not found on this system")
    return None


def detect_java_major_version(java_path: Path) -> int | None:
    """Detect the major version of a Java installation.

    Args:
        java_path: Path to java executable.

    Returns:
        Major version number (e.g. 17, 21, 25) or None if undetectable.
    """
    import re
    import subprocess

    try:
        result = subprocess.run(
            [str(java_path), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stderr + result.stdout
        match = re.search(r'"(\d+)', output)
        if match:
            return int(match.group(1))
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def download_freerouting(target_dir: Path | None = None) -> Path | None:
    """Download a compatible FreeRouting JAR from GitHub releases.

    Detects the installed Java version and downloads a compatible release:
    - Java 25+: downloads latest (v2.x)
    - Java 17-24: downloads v1.9.0 (last version supporting Java 17)

    Args:
        target_dir: Directory to save the JAR. Defaults to ~/.kicad-mcp/freerouting/.

    Returns:
        Path to the downloaded JAR, or None if download failed.
    """
    import urllib.request

    if target_dir is None:
        if os.name == "nt":
            base = Path(os.environ.get("USERPROFILE", str(Path.home())))
        else:
            base = Path.home()
        target_dir = base / ".kicad-mcp" / "freerouting"

    target_dir.mkdir(parents=True, exist_ok=True)

    # Determine compatible version based on Java
    java = find_java()
    java_version = detect_java_major_version(java) if java else None

    if java_version and java_version >= 25:
        jar_name = "freerouting-2.1.0.jar"
        url = "https://github.com/freerouting/freerouting/releases/download/v2.1.0/freerouting-2.1.0.jar"
    else:
        # v1.9.0 is the last version compatible with Java 17
        jar_name = "freerouting-1.9.0.jar"
        url = "https://github.com/freerouting/freerouting/releases/download/v1.9.0/freerouting-1.9.0.jar"

    target_path = target_dir / jar_name
    if target_path.exists():
        logger.info("FreeRouting already downloaded: %s", target_path)
        return target_path

    logger.info("Downloading FreeRouting from %s ...", url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kicad-mcp"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        target_path.write_bytes(data)
        logger.info("Downloaded FreeRouting: %s (%d bytes)", target_path, len(data))
        return target_path
    except Exception as e:
        logger.error("Failed to download FreeRouting: %s", e)
        return None


def get_platform_info() -> dict:
    """Return comprehensive platform information."""
    return {
        "platform": get_platform(),
        "python_version": sys.version,
        "kicad_cli": str(find_kicad_cli()) if find_kicad_cli() else None,
        "kicad_version": detect_kicad_version(),
        "kicad_python_paths": [str(p) for p in find_kicad_python_paths()],
    }
