"""Downloading Frida's Windows gadget: checked against GitHub's digest, written whole."""

import hashlib
import io
import json
import lzma
import struct

import pytest

from hermes.core import wine_gadget
from hermes.core.wine_gadget import GadgetError, ensure_gadget


def _pe(machine):
    header = bytearray(0x40)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    return bytes(header) + b"PE\0\0" + struct.pack("<H", machine) + bytes(64)


def _serve(monkeypatch, dll, digest="right"):
    """Answer the release and the download as GitHub would."""
    compressed = lzma.compress(dll)
    name = "frida-gadget-1.2.3-windows-x86_64.dll.xz"
    if digest == "right":
        digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
    release = {"assets": [{"name": name, "digest": digest}]}

    def urlopen(request, timeout):  # pylint: disable=unused-argument
        url = getattr(request, "full_url", request)
        body = json.dumps(release).encode() if "api.github.com" in url else compressed
        return io.BytesIO(body)

    monkeypatch.setattr(wine_gadget.urllib.request, "urlopen", urlopen)


@pytest.fixture(autouse=True)
def _data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_a_checked_gadget_is_written_whole(monkeypatch, tmp_path):
    dll = _pe(0x8664)
    _serve(monkeypatch, dll)
    path = ensure_gadget(64, version="1.2.3")
    assert path.read_bytes() == dll
    assert not list((tmp_path / "hermes-hook" / "gadget").glob("*.part"))


def test_a_gadget_that_does_not_match_its_digest_is_not_used(monkeypatch):
    _serve(monkeypatch, _pe(0x8664), digest="sha256:" + "0" * 64)
    with pytest.raises(GadgetError, match="SHA-256"):
        ensure_gadget(64, version="1.2.3")
    with pytest.raises(GadgetError, match="not present"):
        ensure_gadget(64, version="1.2.3", download=False)


def test_a_gadget_for_another_architecture_is_not_used(monkeypatch):
    _serve(monkeypatch, _pe(0x14C))
    with pytest.raises(GadgetError, match="no x86_64 DLL"):
        ensure_gadget(64, version="1.2.3")


def test_a_release_without_digests_still_checks_the_dll(monkeypatch):
    _serve(monkeypatch, _pe(0x8664), digest=None)
    assert ensure_gadget(64, version="1.2.3").exists()
