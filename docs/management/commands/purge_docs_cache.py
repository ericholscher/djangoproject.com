import sys

from django.core.cache import caches
from django.core.management.base import BaseCommand

from ...utils import FastlyPurgeError, purge_fastly


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "--doc-versions",
            nargs="+",
            help="Limit purge to these docs versions, if possible; otherwise, "
            "purge everything (support for selective purging is cache-dependent).",
        )

    def handle(self, *args, **options):
        self.verbosity = options["verbosity"]
        doc_versions = set(options["doc_versions"] or [])
        # purge Django first so Fastly doesn't immediately re-cache obsolete pages
        self.purge_django_cache()
        self.purge_fastly(doc_versions)

    def purge_django_cache(self):
        """
        If any doc versions have changed, we need to purge Django's per-site cache
        (in Redis) so any downstream caches don't immediately re-cache obsolete
        versions of the page.

        We have a separate 'docs-pages' cache dedicated to this purpose so
        other pages cached by the cache middleware aren't lost every
        time the docs get rebuilt.
        """
        caches["docs-pages"].clear()

    def purge_fastly(self, doc_versions):
        """
        Purges the Fastly surrogate key for the dev docs if that's
        the only version that's changed, or the entire cache (purge_all)
        if other versions have changed.

        Any errors are echoed to self.stderr even if --verbosity=0 to make
        sure we get an email from cron about them if this task fails for any reason.
        """
        if doc_versions == {"dev"}:
            # If only the dev docs have changed, we can purge only the surrogate key
            # we've set up for the dev docs release in Fastly. This will usually
            # happen with every new commit to django main (on the next hour,
            # when the cron job runs).
            path = "purge/dev-docs-key"
        else:
            # Otherwise, just purge everything, to keep things simple.
            # This will usually only happen around a release when we
            # want these pages to update as soon as possible anyways.
            path = "purge_all"
        if self.verbosity >= 1:
            self.stdout.write(f"Purging Fastly cache: {path}")
        try:
            purge_fastly(path)
        except FastlyPurgeError as e:
            self.stderr.write(f"WARNING: {e}")
            # make sure Ansible sees this as a failure
            sys.exit(1)
