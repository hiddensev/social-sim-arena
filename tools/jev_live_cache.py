"""Encrypt live evidence before it enters a public repository's Actions cache.

Uses a separate age X25519 identity, never the entrant signing key. No keys,
plaintext predictions, or signed bodies are uploaded as artifacts.
"""
import argparse
import io
import os
from pathlib import Path
import tarfile

import pyrage


def pack(directory, archive, identity):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(directory, arcname="state")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(pyrage.encrypt(buffer.getvalue(), [identity.to_public()]))


def unpack(directory, archive, identity):
    plaintext = pyrage.decrypt(archive.read_bytes(), [identity])
    directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "state" or ".." in parts or member.issym() or member.islnk():
                raise ValueError("invalid cache member")
            member.name = str(Path(*parts[1:]))
            tar.extract(member, directory, filter="data")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["pack", "unpack"])
    p.add_argument("--state", type=Path, default=Path(".local/jev/live"))
    p.add_argument("--archive", type=Path, default=Path(".local/jev/encrypted/state.age"))
    args = p.parse_args()
    os.umask(0o077)
    identity = pyrage.x25519.Identity.from_str(os.environ["SSA_JEV_CACHE_AGE_KEY"])
    if args.mode == "pack":
        args.state.mkdir(parents=True, exist_ok=True)
        pack(args.state, args.archive, identity)
    elif args.archive.exists():
        unpack(args.state, args.archive, identity)


if __name__ == "__main__":
    main()
