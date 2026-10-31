
#!/usr/bin/env python3
"""Download upstream IPA releases when their remote assets change."""

import hashlib
import json
import os
import plistlib
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests


# Official release assets.
IPA_SOURCES = {
    "NexStore": "https://github.com/NexSideloading/NexStore/releases/latest/download/NexStore.ipa",
    "SideInstaller": "https://github.com/FrizzleM/SideInstaller/releases/latest/download/SideInstaller.ipa",
    "iRAM+": "https://github.com/NovaDev404/iRAM-Plus/releases/latest/download/iRAM-Plus.ipa",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
IPA_DIR = REPO_ROOT / "ipas"
STATE_FILE = REPO_ROOT / "ipa_state.json"
USER_AGENT = "NexSideloading-IPA-Updater/1.0"


def load_state() -> Dict[str, Any]:
    """Load saved local hashes and remote HTTP validators."""
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if isinstance(state, dict):
            return state

        print("Warning: ipa_state.json is not a JSON object; rebuilding state.")

    except (OSError, json.JSONDecodeError) as exc:
        print(f"Warning: Could not read ipa_state.json ({exc}); rebuilding state.")

    return {}


def save_state(state: Dict[str, Any]) -> None:
    """Write state atomically."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temp_state_file = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")

    with temp_state_file.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")

    os.replace(temp_state_file, STATE_FILE)


def get_file_hash(filepath: Path) -> str:
    """Calculate the SHA-256 hash of a file."""
    digest = hashlib.sha256()

    with filepath.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def get_remote_metadata(
    name: str,
    url: str,
    timeout: int = 30,
) -> Tuple[Optional[Dict[str, Optional[str]]], bool]:
    """Check remote asset metadata without downloading its contents.

    Returns (metadata, not_found).
    """

    headers = {"User-Agent": USER_AGENT}

    try:
        response = requests.head(
            url,
            allow_redirects=True,
            timeout=timeout,
            headers=headers,
        )

        if response.status_code == 404:
            print(f"  {name}: upstream release asset returned 404.")
            return None, True

        # Some hosts do not support HEAD.
        # In that case, fall back to GET.
        if response.status_code in (405, 501):
            print(f"  {name}: HEAD unsupported; checking with GET.")
            return None, False

        response.raise_for_status()

        return {
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "content_length": response.headers.get("Content-Length"),
        }, False

    except requests.exceptions.RequestException as exc:
        print(f"  {name}: HEAD check failed ({exc}); trying download.")
        return None, False


def remote_matches_saved_state(
    remote: Optional[Dict[str, Optional[str]]],
    saved: Dict[str, Any],
    local_path: Path,
) -> bool:
    """Check whether the remote asset and local IPA are unchanged."""

    if not remote or not local_path.is_file():
        return False

    saved_hash = saved.get("hash")

    if not saved_hash:
        # Existing state files don't contain ETags.
        # Download and verify once after installing this update.
        return False

    try:
        if get_file_hash(local_path) != saved_hash:
            return False
    except OSError:
        return False

    remote_size = remote.get("content_length")

    if remote_size:
        try:
            if local_path.stat().st_size != int(remote_size):
                return False
        except (ValueError, OSError):
            return False

    remote_etag = remote.get("etag")
    saved_etag = saved.get("etag")

    # ETags are the strongest available validator.
    if remote_etag and saved_etag:
        return remote_etag == saved_etag

    # Fall back to Last-Modified + Content-Length.
    remote_modified = remote.get("last_modified")
    saved_modified = saved.get("last_modified")
    saved_size = saved.get("content_length")

    if remote_modified and saved_modified and remote_size and saved_size:
        return (
            remote_modified == saved_modified
            and str(remote_size) == str(saved_size)
        )

    # Without a reliable validator, download and compare hashes.
    return False


def validate_ipa(filepath: Path) -> bool:
    """Check that the downloaded file is a structurally valid IPA."""

    try:
        with zipfile.ZipFile(filepath, "r") as archive:
            names = archive.namelist()

            info_plist_path = next(
                (
                    item
                    for item in names
                    if item.startswith("Payload/")
                    and item.endswith(".app/Info.plist")
                ),
                None,
            )

            if info_plist_path is None:
                print("  Downloaded archive has no Payload/*.app/Info.plist.")
                return False

            # Make sure the Info.plist is actually readable.
            plistlib.loads(archive.read(info_plist_path))

        return filepath.stat().st_size > 0

    except (
        OSError,
        zipfile.BadZipFile,
        KeyError,
        plistlib.InvalidFileException,
        ValueError,
    ) as exc:
        print(f"  Downloaded file failed IPA validation: {exc}")
        return False


def download_ipa(
    name: str,
    url: str,
    output_dir: Path,
    timeout: Tuple[int, int] = (15, 180),
) -> Tuple[Optional[Path], Dict[str, Optional[str]]]:
    """Download and validate an IPA into a temporary file.

    The existing IPA is never touched until the new download has
    successfully completed validation.
    """

    temp_path = output_dir / f".{name}.ipa.tmp"
    headers = {"User-Agent": USER_AGENT}

    try:
        temp_path.unlink(missing_ok=True)

        print(f"  Downloading {name}...")

        with requests.get(
            url,
            stream=True,
            allow_redirects=True,
            timeout=timeout,
            headers=headers,
        ) as response:

            if response.status_code == 404:
                print(f"  {name}: release asset returned 404; keeping existing copy.")
                return None, {}

            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "").lower()

            if content_type.startswith(
                ("text/", "application/json", "application/xml")
            ):
                print(f"  {name}: expected an IPA but received {content_type}.")
                return None, {}

            with temp_path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file.write(chunk)

            metadata = {
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "content_length": response.headers.get("Content-Length"),
            }

        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            print(f"  {name}: downloaded file is empty.")
            temp_path.unlink(missing_ok=True)
            return None, {}

        if not validate_ipa(temp_path):
            temp_path.unlink(missing_ok=True)
            return None, {}

        print(f"  {name}: download and IPA validation successful.")

        return temp_path, metadata

    except requests.exceptions.Timeout:
        print(f"  {name}: download timed out; keeping existing copy.")

    except requests.exceptions.RequestException as exc:
        print(f"  {name}: download failed ({exc}); keeping existing copy.")

    except OSError as exc:
        print(f"  {name}: file error ({exc}); keeping existing copy.")

    except Exception as exc:
        print(f"  {name}: unexpected download error ({exc}); keeping existing copy.")

    temp_path.unlink(missing_ok=True)
    return None, {}


def main() -> list[str]:
    """Check every upstream release and return successfully changed IPA names."""

    IPA_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    changed_ipas: list[str] = []

    print("Checking upstream IPA releases for updates...")
    print("=" * 60)

    for name, url in IPA_SOURCES.items():

        output_path = IPA_DIR / f"{name}.ipa"

        previous_state = state.get(name, {})

        if not isinstance(previous_state, dict):
            previous_state = {}

        print(f"Checking {name}...")

        # IMPORTANT:
        # Check the REMOTE release on every run.
        #
        # The old script only compared the local file against
        # its saved local hash, meaning it could never discover
        # a new upstream IPA.

        remote_metadata, not_found = get_remote_metadata(name, url)

        if not_found:
            print(f"  {name}: leaving existing file untouched.")
            continue

        # Skip downloading only when the remote validator and
        # local file both confirm that nothing has changed.

        if remote_matches_saved_state(
            remote_metadata,
            previous_state,
            output_path,
        ):
            print(f"  {name}: remote asset and local IPA are unchanged.")
            continue

        # Download the asset into a temporary file.

        temp_path, download_metadata = download_ipa(
            name,
            url,
            IPA_DIR,
        )

        if temp_path is None:
            # Do not update state or report a change when a
            # download fails. The next workflow run will retry.
            continue

        try:
            downloaded_hash = get_file_hash(temp_path)

            existing_hash = (
                get_file_hash(output_path)
                if output_path.is_file()
                else None
            )

            if downloaded_hash == existing_hash:

                print(f"  {name}: downloaded asset is identical; no replacement needed.")

                temp_path.unlink(missing_ok=True)

            else:

                # Atomic replacement.
                # The old IPA remains intact until the new one
                # has been downloaded, validated and hashed.

                os.replace(temp_path, output_path)

                changed_ipas.append(name)

                print(
                    f"  {name}: updated successfully "
                    f"({output_path.stat().st_size:,} bytes)."
                )

            # Prefer headers from the actual GET response.
            # Fall back to the HEAD response when necessary.

            etag = (
                download_metadata.get("etag")
                or (remote_metadata or {}).get("etag")
            )

            last_modified = (
                download_metadata.get("last_modified")
                or (remote_metadata or {}).get("last_modified")
            )

            content_length = (
                download_metadata.get("content_length")
                or (remote_metadata or {}).get("content_length")
                or str(output_path.stat().st_size)
            )

            state[name] = {
                "hash": downloaded_hash,
                "last_updated": str(output_path.stat().st_mtime),
                "etag": etag,
                "last_modified": last_modified,
                "content_length": content_length,
            }

        except OSError as exc:

            print(f"  {name}: could not install downloaded IPA ({exc}).")

            temp_path.unlink(missing_ok=True)

    # Save updated state.
    save_state(state)

    print("=" * 60)
    print(
        f"IPA check completed. Successfully changed IPAs: "
        f"{len(changed_ipas)}"
    )

    if changed_ipas:
        print("Changed apps: " + ", ".join(changed_ipas))
    else:
        print("No IPA changes detected.")

    # run_all.py uses this list to tell sign_apps.py
    # which apps need to be re-signed.

    return changed_ipas


if __name__ == "__main__":
    main()
