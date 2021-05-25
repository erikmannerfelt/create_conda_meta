"""
A tool to automatically generate conda-build recipes from GitHub projects.

Author(s):
    Erik Mannerfelt (@erikmannerfelt)

Date:
    25-05-2021

Usage:
    >>> python make_meta_yml.py -h
"""
import argparse
import hashlib
import io
import os
import sys
import tarfile
import tempfile
import warnings
from typing import List, Tuple

import requests
import setuptools
import yaml


def get_latest_release(username: str, repo: str) -> str:
    """Get the latest release tag from a github repository."""
    response = requests.get(
        f"https://api.github.com/repos/{username}/{repo}/releases/latest",
        allow_redirects=True,
    )

    tag_name = response.json()["tag_name"]

    return tag_name


def download_tar_gz(username: str, repo: str, tag_name: str) -> requests.Response:
    """Download the tar.gz source from a specific tag in a repo."""
    url = f"https://github.com/{username}/{repo}/archive/refs/tags/{tag_name}.tar.gz"

    response = requests.get(url, allow_redirects=True)
    response.raise_for_status()

    return response


def extract_tar_gz(response: requests.Response) -> tempfile.TemporaryDirectory:
    """Extract a tar.gz source from a response and return the filepath."""
    temp_dir = tempfile.TemporaryDirectory()

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        tar.extractall(temp_dir.name)

    return temp_dir


def parse_license(extracted_tar_gz_path: str) -> Tuple[str, str]:
    """Parse a license type from a LICENSE file in the repo."""
    # Try to find a file with "license" in its name
    for root, _, files in os.walk(extracted_tar_gz_path):

        potential_license = [
            filename for filename in files if "license" in filename.lower()
        ]

        if len(potential_license) > 0:
            filepath = os.path.join(root, potential_license[0])
            break
    else:
        raise ValueError("Could not find license file.")

    # Read the license file
    with open(os.path.join(extracted_tar_gz_path, filepath)) as infile:
        content = infile.read()

    # Count the occurrences of strings to find the one that occurs most
    lower_content = content.lower()
    licenses = {
        "MIT": content.count("MIT"),
        "Apache-2.0": lower_content.count("apache"),
        "GPL-2.0-or-later": content.count("GNU"),
        "BSD-3-Clause": lower_content.count("bsd 3"),
    }

    # Stop if none of the keys were found.
    if sum(licenses.values()) == 0:
        raise ValueError(
            f"Could not find a license in {filepath}. Looked for: {licenses.keys()}"
        )

    most_likely = max(licenses, key=lambda key: licenses[key])

    return most_likely, os.path.basename(filepath)


def get_requirements(extracted_tar_gz_path: str) -> List[str]:
    """Get requirements from a requirements file."""
    for root, _, files in os.walk(extracted_tar_gz_path):

        potential_reqs = [
            filename for filename in files if "requirements" in filename.lower()
        ]

        if len(potential_reqs) > 0:
            filepath = os.path.join(root, potential_reqs[0])
            break
    else:
        raise ValueError("Could not find requirements file.")

    with open(filepath) as infile:
        requirements = infile.read().splitlines()

    return requirements


def parse_setup_py(
    extracted_tar_gz_path: str,
) -> setuptools.distutils.core.Distribution:
    """Parse the setup.py file and return the metadata."""
    for root, _, files in os.walk(extracted_tar_gz_path):

        potential_setup = [
            filename for filename in files if "setup" in filename.lower()
        ]

        if len(potential_setup) > 0:
            filepath = os.path.join(root, potential_setup[0])
            break
    else:
        raise ValueError("Could not find setup.py file.")

    current_dir = os.getcwd()

    # Change to the base directory
    os.chdir(os.path.dirname(filepath))
    # Insert the current directory in the beginning of the path, so "import xxx" will find
    # relative imports first
    sys.path.insert(0, os.path.dirname(filepath))
    distribution = setuptools.distutils.core.run_setup(filepath, stop_after="config")
    os.chdir(current_dir)

    return distribution


class CondaYamlDumper(yaml.SafeDumper):
    """conda recipe-specific formatting options."""

    def write_line_break(self, data=None):
        """
        Write line breaks between top-level keys.

        Taken from: https://github.com/yaml/pyyaml/issues/127#issuecomment-525800484
        """
        super().write_line_break(data)

        if len(self.indents) == 1:
            super().write_line_break()


def validate_maintainers(maintainers: List[str]) -> None:
    """Check that all maintainers are valid GitHub users."""
    for maintainer in maintainers:
        try:
            requests.get(
                f"https://api.github.com/users/{maintainer}"
            ).raise_for_status()
        except requests.exceptions.HTTPError as exception:
            if "404" in str(exception):
                raise ValueError(f"'{maintainer}' could not be found as a GitHub user")
            raise exception

def validate_urls(urls: List[str]) -> None:
    """Validate that each URL returns a 200 status."""
    for url in urls:
        # The URL fields are allowed to be empty
        if len(url) == 0:
            continue
        status = requests.get(url).status_code

        if status != 200:
            raise ValueError(f"URL: {url} returned non-200 status code: {status}")


def make_meta_yaml(
    username: str, repo: str, maintainers: List[str], documentation_url: str = ""
):
    """
    Create a meta.yaml file from a GitHub hosted project to use with conda-build.

    :param username: The username or organization name for the repository.
    :param repo: The repository name.
    :param maintainers: A list of maintainer github names.
    :param documentation_url: Optional. URL to the documentation.
    """
    github_url = f"https://github.com/{username}/{repo}"
    tag_name = get_latest_release(username, repo)
    response = download_tar_gz(username, repo, tag_name)
    extracted_repo = extract_tar_gz(response)

    repo_license, license_filename = parse_license(extracted_repo.name)
    requirements = get_requirements(extracted_repo.name)
    distribution_info = parse_setup_py(extracted_repo.name)

    version = distribution_info.get_version()
    home_url = distribution_info.get_url()
    package_name = distribution_info.get_name()
    summary = distribution_info.get_description()

    sha256 = hashlib.sha256(response.content).hexdigest()

    validate_maintainers(maintainers)
    validate_urls([github_url, home_url, documentation_url])
    if version not in tag_name:
        warnings.warn(
            f"Version '{version}' in setup.py may not correspond to release tag name: '{tag_name}'"
        )

    preamble = "\n".join(
        [
            "{" + f"% set name = '{package_name}' %" + "}",
            "{" + f"% set version = '{version}' %" + "}",
        ]
    )

    meta = {
        "package": {"name": "{{ name | lower }}", "version": "{{ version }}"},
        "source": {
            "url": f"https://github.com/{username}/{repo}/archive/refs/tags/{tag_name}.tar.gz",
            "sha256": sha256,
        },
        "build": {"number": 0, "script": "{{ PYTHON }} -m pip install . -vv"},
        "requirements": {
            "host": ["python", "pip"] + requirements,
            "run": ["python"] + requirements,
        },
        "test": {"imports": [package_name]},
        "about": {
            "home": home_url,
            "license": repo_license,
            "license_file": license_filename,
            "summary": summary,
            "doc_url": documentation_url,
            "dev_url": github_url,
        },
        "extra": {
            "recipe_maintainers": maintainers,
        },
    }

    meta_yml_string = (
        preamble + "\n\n" + yaml.dump(meta, sort_keys=False, Dumper=CondaYamlDumper)
    )

    return meta_yml_string


def cli():
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(prog="make_meta_yml")

    parser.add_argument(
        "username", type=str, help="User or organization name for the repo"
    )
    parser.add_argument("repo", type=str, help="Name of the repository")
    parser.add_argument(
        "maintainers",
        type=str,
        nargs="+",
        help="The GitHub users to set as maintainers.",
    )
    parser.add_argument(
        "--doc_url", type=str, help="Documentation URL", default="", required=False
    )

    args = parser.parse_args()

    meta_yml_string = make_meta_yaml(
        username=args.username,
        repo=args.repo,
        maintainers=args.maintainers,
        documentation_url=args.doc_url,
    )

    sys.stdout.write(meta_yml_string)



if __name__ == "__main__":
    cli()
