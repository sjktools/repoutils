"""Check that PyPI's provenance for a release names the expected GitHub
repository as the trusted publisher of every file, for `downstream bump`.
It reads TestPyPI instead when given its URL, for a test build.

A downstream's Dependabot cooldown doesn't apply to a bump that its upstream
pushes, so this checks the release first. PyPI's integrity API gives each
file's attestation bundles, each naming the trusted publisher that uploaded
the file, and PyPI has already verified their signatures. Comparing the name
needs only the standard library, where verifying the signatures again would
need sigstore."""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PYPI_URL = "https://pypi.org"
TESTPYPI_URL = "https://test.pypi.org"
_NAMES = {PYPI_URL: "PyPI", TESTPYPI_URL: "TestPyPI"}
# How often, and how far apart, a release that PyPI doesn't know yet is asked
# for again, as its JSON API can lag behind the upload for a minute or two.
ATTEMPTS = 5
DELAY = 30.0


def get_json(url: str) -> Any:
    """The JSON at url, which raises HTTPError on a 404."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def index_name(index: str) -> str:
    """The name of the index at the URL index, for messages."""
    return _NAMES.get(index, index)


def _quote(part: str) -> str:
    return urllib.parse.quote(part, safe="")


def _release(url: str, name: str, package: str, version: str) -> Any:
    """The JSON the index name gives for a release, asking again while it says it has no such release."""
    attempt = 1
    while True:
        try:
            return get_json(url)
        except urllib.error.HTTPError as e:
            e.close()
            if e.code != 404:
                raise RuntimeError(f"{name} answered HTTP {e.code} for {url}") from e
            if attempt >= ATTEMPTS:
                raise RuntimeError(f"{name} has no release {version} of {package} ({url})") from e
        print(f"{name} has no {package} {version} yet, so asking again in {DELAY:g}s", file=sys.stderr)
        time.sleep(DELAY)
        attempt += 1


def release_files(package: str, version: str, index: str = PYPI_URL) -> list[str]:
    """The names of the files the index at the URL index has for package version."""
    name = index_name(index)
    release = _release(f"{index}/pypi/{_quote(package)}/{_quote(version)}/json", name, package, version)
    files = [str(file["filename"]) for file in release.get("urls", [])]
    if not files:
        raise RuntimeError(f"{name}'s release {version} of {package} has no files")
    return files


def publishers(package: str, version: str, filename: str, index: str = PYPI_URL) -> list[dict[str, Any]]:
    """The trusted publishers that the provenance for the file, on the index at the URL index, names."""
    name = index_name(index)
    url = f"{index}/integrity/{_quote(package)}/{_quote(version)}/{_quote(filename)}/provenance"
    try:
        provenance = get_json(url)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 404:
            raise RuntimeError(
                f"{name} has no provenance for {filename}, so it wasn't uploaded through trusted publishing "
                "with attestations"
            ) from e
        raise RuntimeError(f"{name} answered HTTP {e.code} for {url}") from e
    return [bundle.get("publisher", {}) for bundle in provenance.get("attestation_bundles", [])]


def check(package: str, version: str, repository: str, index: str = PYPI_URL) -> list[str]:
    """Raise RuntimeError unless the provenance on the index at the URL index
    names the GitHub repository as the publisher of each of package version's
    files, and of nothing else. Returns the files' names."""
    name = index_name(index)
    files = release_files(package, version, index)
    for filename in files:
        found = publishers(package, version, filename, index)
        if not found:
            raise RuntimeError(f"{name}'s provenance for {filename} has no attestation bundles")
        for publisher in found:
            # GitHub's repository names are case-insensitive.
            if publisher.get("kind") != "GitHub" or str(publisher.get("repository", "")).lower() != repository.lower():
                raise RuntimeError(
                    f"{name}'s provenance for {filename} names {publisher.get('kind')} "
                    f"{publisher.get('repository')!r} as its publisher, not GitHub repository {repository}"
                )
    return files
