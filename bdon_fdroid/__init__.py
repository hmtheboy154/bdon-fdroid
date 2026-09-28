"""Scrape the official BanG Dream! Our Notes website and publish an F-Droid repo.

The project is deliberately dependency-free: everything here uses the Python
standard library so the daily GitHub Actions job needs no ``pip install`` step.
"""

__version__ = "1.0.0"

#: Package name of the APK the website serves. Note this differs from the
#: Google Play listing (`com.bilibili.sirius`): the site distributes the
#: regional "official" build, which carries its own application id. F-Droid keys
#: everything on the package name, and the publisher's own client would refuse
#: to update across the two, so the site's build is what gets indexed.
PACKAGE_NAME = "com.bilibili.sirius.official"

SITE_URL = "https://bdon.biligames.com/"
