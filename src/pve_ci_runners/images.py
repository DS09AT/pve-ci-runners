"""The cloud image a template build starts from, downloaded once and kept under its checksum."""

import hashlib
import logging
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast

from pve_ci_runners.errors import PveCiError

_CHUNK = 2**20
# The checksum is known only at the end, so the size bounds what a hostile server can write to the host.
_MAX_BYTES = 8 * 2**30
log = logging.getLogger("pve_ci_runners")

Opener = Callable[[str], BinaryIO]


def _open(url: str) -> BinaryIO:
    # The URL is validated as https by the configuration, and the pinned checksum decides what is used.
    return cast("BinaryIO", urllib.request.urlopen(url, timeout=60))  # noqa: S310 - https only


def fetch_image(url: str, sha256: str, directory: Path, *, opener: Opener = _open) -> Path:
    """The image file with exactly this checksum; older images in the directory are removed."""
    target = directory / f"{sha256}.img"
    if not (target.is_file() and _digest_of(target) == sha256):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        log.info("downloading the base image %s", url)
        try:
            _download(url, sha256, partial, opener)
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    for other in directory.glob("*.img"):
        if other != target:
            other.unlink()
    return target


def _download(url: str, sha256: str, partial: Path, opener: Opener) -> None:
    digest = hashlib.sha256()
    size = 0
    with opener(url) as response, partial.open("wb") as out:
        while chunk := response.read(_CHUNK):
            size += len(chunk)
            if size > _MAX_BYTES:
                raise PveCiError(f"the image at {url} is larger than {_MAX_BYTES} bytes")
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != sha256:
        raise PveCiError(f"the image at {url} does not have the configured sha256")


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
