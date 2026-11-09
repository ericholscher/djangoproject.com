import logging
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.cache import caches
from django.http import Http404
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Fastly surrogate key set on every rendered docs page (but not static assets).
DOCS_HTML_SURROGATE_KEY = "docs-html"


class FastlyPurgeError(Exception):
    pass


def purge_fastly(path, *, soft=False, retries=5):
    """
    Send a purge request to the docs Fastly service, e.g. ``purge_all`` or
    ``purge/<surrogate key>``. Requires these settings:

    * settings.FASTLY_SERVICE_URL: the full URL to the "Django Docs" Fastly
      service API endpoint e.g.,
      https://api.fastly.com/service/SU1Z0isxPaozGVKXdv0eY/
      (a trailing slash will be added for you if you don't supply one)
    * settings.FASTLY_API_KEY: your Fastly API key with "purge_all" and
      "purge_select" scope for the above Django Docs service

    A soft purge marks the content stale instead of removing it, so Fastly
    keeps serving it while it refetches from the origin.

    Raise FastlyPurgeError if the settings are missing or the purge fails.
    """
    service_url = getattr(settings, "FASTLY_SERVICE_URL", None)
    api_key = getattr(settings, "FASTLY_API_KEY", None)
    if not (service_url and api_key):
        raise FastlyPurgeError(
            "Fastly API key and/or service URL not found; can't purge cache"
        )
    # Make sure service_url ends with a trailing slash; otherwise, urljoin()
    # will lop off the last part of the path. If needed, urljoin() will remove
    # any duplicate slashes for us.
    url = urljoin(service_url + "/", path)
    session = requests.Session()
    # Make some allowance for temporary network failures.
    retry = Retry(total=retries, allowed_methods={"POST"}, backoff_factor=0.1)
    session.mount(url, HTTPAdapter(max_retries=retry))
    headers = {"Fastly-Key": api_key, "Accept": "application/json"}
    if soft:
        headers["Fastly-Soft-Purge"] = "1"
    try:
        result = session.post(url, headers=headers, timeout=5).json()
    except (requests.RequestException, ValueError) as e:
        raise FastlyPurgeError(f"Fastly purge failed for URL: {url}; {e}") from e
    if result.get("status") != "ok":
        raise FastlyPurgeError(f"Fastly purge failed for URL: {url}; result={result}")


def purge_docs_html():
    """
    Purge rendered docs pages from Django's docs-pages cache and Fastly.

    Used when something shared by every docs page changes, such as the site
    banner. Django's cache is cleared first so Fastly doesn't re-cache the old
    HTML from it, then the docs-html surrogate key is soft purged so Fastly
    doesn't send every docs page to the origin at once.

    Errors are logged rather than raised so that callers, like a banner save
    in the admin, aren't broken by a CDN outage.
    """
    caches["docs-pages"].clear()
    try:
        purge_fastly(f"purge/{DOCS_HTML_SURROGATE_KEY}", soft=True, retries=1)
    except FastlyPurgeError:
        logger.exception("Couldn't purge rendered docs pages from Fastly")


try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None


def capture_sentry_exception(error, flush=False):
    """Report an exception to Sentry if sentry_sdk is available.

    Pass flush=True to block until the event is sent (use before process
    exit). Returns True if sent to Sentry, False if sentry_sdk is not
    installed.
    """
    if sentry_sdk is None:
        return False
    sentry_sdk.capture_exception(error)
    if flush:
        sentry_sdk.flush()
    return True


def get_doc_root(lang, version, builder="json"):
    return settings.DOCS_BUILD_ROOT / lang / version / "_built" / builder


def get_doc_root_or_404(lang, version, builder="json"):
    docroot = get_doc_root(lang, version, builder)
    if not docroot.exists():
        raise Http404(str(docroot))
    return docroot


def get_doc_path(docroot, subpath):
    # First look for <bits>/index.fjson, then for <bits>.fjson
    try:
        bits = subpath.strip("/").split("/") + ["index.fjson"]
    except AttributeError:
        bits = []
    doc = docroot / Path(*bits)
    try:
        if doc.exists():
            return doc
    except NotADirectoryError:
        pass  # we get here if doc + subpath (without /index.fjson) is a file

    bits = bits[:-2] + ["%s.fjson" % bits[-2]]
    doc = docroot / Path(*bits)
    if doc.exists():
        return doc

    return None


def get_doc_path_or_404(docroot, subpath):
    doc = get_doc_path(docroot, subpath)
    if doc is None:
        raise Http404(doc)
    return doc


def sanitize_for_trigram(text):
    """
    Sanitize search query for PostgreSQL Trigram search.

    - Removes parts starting with '-'
    - Normalizes Unicode characters (NFKD)
    - Keeps only letters, numbers and spaces
    - Removes multiple spaces and trims
    """
    text = re.sub(r'(\s|^)-[^\s"\']+|(\s|^)-["\'][^"\']+["\']', "", text)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return " ".join(text.split())


def get_module_path(name, full_path):
    """
    Checks if the `full_path` ends with `.name` and, if so, removes it to return
    the module path. Otherwise, it returns `None`.

    Args:
        name (str):
            The short name of the object (e.g., `"QuerySet.select_related"`).
        full_path (str):
            The full path of the object (e.g.,
            `"django.db.models.query.QuerySet.select_related"`).

    Returns:
        str or None:
            The module path if `full_path` ends with `.name`, otherwise `None`.

    Example:
        >>> get_module_path(
        ...   "QuerySet.select_related",
        ...   "django.db.models.query.QuerySet.select_related"
        ... )
        'django.db.models.query'

        >>> get_module_path("Model", "django.db.models.Model")
        'django.db.models'

        >>> get_module_path("django", "django")
        None
    """
    name_suffix = f".{name}"
    if full_path.endswith(name_suffix):
        return full_path.removesuffix(name_suffix)
    return None


class SingleTagInnerHTMLExtractor(HTMLParser):
    def __init__(self, target_tag):
        super().__init__()
        self.target_tag = target_tag.lower()
        self.capturing = False
        self.inner_html = []
        self.tag_count = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == self.target_tag:
            self.tag_count += 1
            if self.capturing:
                # Nested target tag not allowed.
                raise ValueError(f"Nested <{self.target_tag}> tags are not allowed.")
            self.capturing = True
        elif self.capturing:
            self.inner_html.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.capturing:
            if tag == self.target_tag:
                self.capturing = False
            else:
                self.inner_html.append(f"</{tag}>")

    def handle_data(self, data):
        if self.capturing:
            self.inner_html.append(data)

    def handle_entityref(self, name):
        if self.capturing:
            self.inner_html.append(f"&{name};")

    def handle_charref(self, name):
        if self.capturing:
            self.inner_html.append(f"&#{name};")


def extract_inner_html(html, tag):
    """
    Extracts the inner HTML of a tag that appears exactly once.
    """
    parser = SingleTagInnerHTMLExtractor(tag)
    parser.feed(html)
    parser.close()

    if parser.tag_count == 0:
        raise ValueError(f"<{tag}> not found in HTML.")
    if parser.tag_count > 1:
        raise ValueError(f"<{tag}> occurs more than once in HTML.")

    return "".join(parser.inner_html)
