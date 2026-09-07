"""Base image build and fingerprinting (spec R-11).

Every file under image/ (Dockerfile, lockfiles, template files) is hashed; a
changed hash or a missing image triggers a rebuild. This avoids the stale-image
trap where an edited Dockerfile or lockfile keeps serving the previously built
image.
"""

import hashlib
import json
import subprocess
import sys

from . import config
from .errors import ImageError


def fingerprint():
    if not config.DOCKERFILE.exists():
        raise ImageError(
            f"Dockerfile missing at {config.DOCKERFILE}",
            "Reinstall agent-sandbox or restore the image/Dockerfile file.",
        )
    h = hashlib.sha256()
    files = sorted(
        p for p in config.IMAGE_DIR.rglob("*")
        if p.is_file() and "node_modules" not in p.relative_to(config.IMAGE_DIR).parts
    )
    for p in files:
        rel = p.relative_to(config.IMAGE_DIR).as_posix()
        h.update(rel.encode("utf-8") + b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _state():
    if config.IMAGE_STATE.exists():
        try:
            return json.loads(config.IMAGE_STATE.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def _save_state(data):
    config.RUNS.mkdir(parents=True, exist_ok=True)
    tmp = config.IMAGE_STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(config.IMAGE_STATE)


def exists(image=None):
    image = image or config.IMAGE_NAME
    p = subprocess.run(["docker", "image", "inspect", image],
                       capture_output=True, text=True, env=config.docker_env())
    return p.returncode == 0


def needs_build(image=None):
    image = image or config.IMAGE_NAME
    if not exists(image):
        return True, "image not present"
    if _state().get(image) != fingerprint():
        return True, "image/ changed since last build"
    return False, "up to date"


def build(image=None, no_cache=False, quiet=False, stream=True):
    """Build the base image. Returns True on success."""
    image = image or config.IMAGE_NAME
    args = ["docker", "build", "-t", image, "-f", str(config.DOCKERFILE)]
    if no_cache:
        args.append("--no-cache")
    args.append(str(config.IMAGE_DIR))

    if not quiet:
        print(f"[agent-sandbox] building {image} (first run takes a few minutes)...",
              file=sys.stderr, flush=True)

    if stream and not quiet:
        p = subprocess.run(args, env=config.docker_env())
        rc = p.returncode
        err = ""
    else:
        p = subprocess.run(args, capture_output=True, text=True,
                           env=config.docker_env())
        rc = p.returncode
        err = p.stderr

    if rc != 0:
        raise ImageError(
            f"image build failed{': ' + err.strip()[-800:] if err else ''}",
            "Run `agent-sandbox build` to see the full build output, and check "
            "network access to the apt/npm registries.",
        )

    st = _state()
    st[image] = fingerprint()
    _save_state(st)
    if not quiet:
        print(f"[agent-sandbox] built {image}", file=sys.stderr, flush=True)
    return True


def ensure(image=None, quiet=False):
    """Auto-build transparently on first use (R-11)."""
    image = image or config.IMAGE_NAME
    need, why = needs_build(image)
    if need:
        if not quiet:
            print(f"[agent-sandbox] {why}", file=sys.stderr, flush=True)
        build(image, quiet=quiet)
    return image
