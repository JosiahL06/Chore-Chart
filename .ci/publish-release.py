#!/usr/bin/env python3
"""Publish the CHANGELOG.md section for a version tag as that tag's release body.

Called by the release-notes workflow; the body includes the matching
`docker pull` line for the version's image. Required environment:

    TAG       version tag to publish, e.g. v0.2.1
    REPO      owner/name of the repository the release belongs to
    TOKEN     API token for the host running the job
    REGISTRY  registry prefix for the body's docker pull line, e.g. ghcr.io

Optional environment:

    RERELEASE set (non-empty, and not 0/false/no) to update the body of an
              existing release in place instead of leaving it untouched --
              the manual re-release path. Falls back to creating the release
              when the tag was never released.

Exits 0 when the release is published, when it already exists (re-runs are
no-ops), or when CHANGELOG.md has no section for TAG (warning only); non-zero
when the API fails.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def require(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} environment variable is required")
    return value


tag = require("TAG")
repo = require("REPO")
token = require("TOKEN")
registry = require("REGISTRY")


def api_base():
    """API root as seen from *this job container*.

    A job container's DNS may not resolve the host it cloned from, so
    never hardcode an API URL: prefer the URL the runner was configured
    with, and fall back to the URL the checkout step just cloned from.
    """
    for name in ("GITHUB_API_URL", "GITEA_API_URL"):
        value = os.environ.get(name, "").strip()
        if value.startswith("http"):
            return value.rstrip("/")
    for name in ("GITHUB_SERVER_URL", "GITEA_SERVER_URL"):
        value = os.environ.get(name, "").strip().rstrip("/")
        if value.startswith("http"):
            return f"{value}/api/v1"
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    match = re.match(r"^(https?://)(?:[^@/]*@)?([^/]+)/", remote)
    return f"{match.group(1)}{match.group(2)}/api/v1" if match else ""


api = api_base()
if not api:
    sys.exit("cannot work out the API URL from this job")
url = f"{api}/repos/{repo}/releases"

# The changelog section for this tag, up to the next version heading.
section, capture = [], False
for line in Path("CHANGELOG.md").read_text(encoding="utf-8").splitlines():
    if line.startswith(f"## [{tag}]"):
        capture = True
        continue
    if capture and line.startswith("## ["):
        break
    if capture:
        section.append(line)
notes = "\n".join(section).strip()
if not notes:
    print(f"::warning::CHANGELOG.md has no '## [{tag}]' section -- not publishing a release")
    sys.exit(0)

# The image tag comes from VERSION (that is what CI tags images with), so
# a tag whose VERSION disagrees still points at a pullable image.
try:
    version = Path("VERSION").read_text(encoding="utf-8").strip() or tag
except OSError:
    version = tag
body = (
    f"{notes}\n\n"
    "## Container image\n\n"
    "```bash\n"
    f"docker pull {registry}/{repo.lower()}:{version}\n"
    "```\n\n"
    "`:latest` tracks the newest build; every build is also tagged with its git sha."
)

# RERELEASE (the manual dispatch path) refreshes an existing release's body; a
# plain tag push only ever creates, so a re-run there stays a no-op.
rerelease = os.environ.get("RERELEASE", "").strip().lower() not in ("", "0", "false", "no")

headers = {
    "Authorization": f"token {token}",
    "Content-Type": "application/json",
}


def call(method, endpoint, payload=None):
    """Call the release API; return (status, parsed-json-or-body-text)."""
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request) as response:
            text = response.read().decode(errors="replace")
            return response.status, json.loads(text) if text.strip() else None
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


if rerelease:
    # Update the notes of a release that already exists; fall through to
    # create it when this version was never released before.
    status, existing = call("GET", f"{url}/tags/{tag}")
    if status == 200 and isinstance(existing, dict):
        status, result = call(
            "PATCH", f"{url}/{existing['id']}", {"name": tag, "body": body}
        )
        if 200 <= status < 300:
            print(f"updated release {tag} (HTTP {status})")
            sys.exit(0)
        sys.exit(f"release API returned HTTP {status}: {result}")
    if status != 404:
        sys.exit(f"release API returned HTTP {status} looking up release {tag}: {existing}")

print(f"POST {url}")
# draft=True would leave it unpublished for review in the forge's web UI.
status, result = call("POST", url, {"tag_name": tag, "name": tag, "body": body})
if 200 <= status < 300:
    print(f"published release {tag} (HTTP {status})")
    sys.exit(0)
# A duplicate release comes back as 409 or as 422 with an "already_exists"
# validation error, depending on the host -- both mean "this tag was released
# before", so a re-run stays a no-op.
if isinstance(result, str) and (
    status == 409 or (status == 422 and "already_exists" in result)
):
    print(f"release {tag} already exists -- left untouched")
    sys.exit(0)
sys.exit(f"release API returned HTTP {status}: {result}")
