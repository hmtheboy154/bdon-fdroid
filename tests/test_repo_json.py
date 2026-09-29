"""Tests for the configuration this repository actually ships.

``repo.json`` is the one file every build reads but nothing tested, which is how
it ended up claiming Craft Egg made the game. These tests pin the facts that
are expensive to get wrong and impossible to notice from a build log:

* the description is **HTML**, because both F-Droid clients parse it as such and
  a plain-text paste collapses into one wall of text;
* it is within F-Droid's own field limits, tags included;
* the developer, publisher and trademark holder are named correctly;
* the repository presents itself as unofficial, because it is;
* the defaults in :mod:`bdon_fdroid.config` still agree with the file, so a fork
  that deletes ``repo.json`` gets the same repository rather than a different one.
"""

import json
import pathlib
import unittest
from html.parser import HTMLParser

from bdon_fdroid import config as config_module
from bdon_fdroid.config import CHAR_LIMITS, Config, ConfigError

REPO_JSON = pathlib.Path(__file__).resolve().parent.parent / "repo.json"

#: The categories the *current* F-Droid client puts in its "Games" group, taken
#: from ``CategoryGroups.games`` in fdroidclient. There is no category called
#: "Games": that is the name of the group, and a category with that literal name
#: falls through to the client's ``else -> CategoryGroups.misc`` branch, so the
#: app would show up under Misc.
GAMES_GROUP = {
    "Action Game",
    "Board Game",
    "Card Game",
    "Casual Game",
    "Dice",
    "Educational Game",
    "Emulator",
    "Game Helper",
    "Party Game",
    "Platformer Game",
    "Puzzle Game",
    "Role-Playing Game",
    "Shooter Game",
    "Sport Game",
    "Strategy Game",
    "Visual Novel",
    "Word Game",
}


class _Balanced(HTMLParser):
    """A tag-balance checker, deliberately tiny.

    ``<br>`` is void in HTML, so an ``endtag`` for it is an error; everything
    else must close in order. This is only here to catch a half-finished edit
    to the description, not to validate the markup.
    """

    VOID = {"br", "img", "hr", "meta", "link"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.problems = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closes {self.stack!r}")
        else:
            self.stack.pop()


class ShippedConfigTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(REPO_JSON.read_text(encoding="utf-8"))
        self.app = self.data["app"]
        self.repo = self.data["repo"]

    # --- the description is HTML ------------------------------------------
    def test_description_is_well_formed(self):
        parser = _Balanced()
        parser.feed(self.app["description"])
        self.assertEqual(parser.problems, [], "unbalanced description")
        self.assertEqual(parser.stack, [], "description leaves tags open")

    def test_description_uses_paragraphs_not_bare_newlines(self):
        # A client collapses whitespace, so paragraphs only survive as markup.
        self.assertNotIn("\n", self.app["description"])
        self.assertIn("<p>", self.app["description"])
        self.assertIn("<br>", self.app["description"])

    def test_description_escapes_nothing_it_should_not(self):
        # & and < would be eaten by the HTML parser if they were left bare.
        # The em dashes and curly quotes are fine: the index is UTF-8.
        text = self.app["description"]
        self.assertNotIn("&mdash", text)
        self.assertNotIn("&ldquo", text)
        self.assertIn("\u2014", text)  # em dash
        self.assertIn("\u2019", text)  # right single quote

    def test_description_keeps_the_publishers_own_copyright_line(self):
        self.assertIn("\u00a9BanG Dream! Project \u00a9FROMTOKYO \u00a9Bushiroad", self.app["description"])
        self.assertIn("Published by BILIBILI HK LIMITED", self.app["description"])

    # --- field limits ----------------------------------------------------
    def test_description_is_within_fdroids_limit(self):
        limit = CHAR_LIMITS["description"]
        self.assertLessEqual(len(self.app["description"]), limit)

    def test_summary_and_author_are_within_fdroids_limits(self):
        self.assertLessEqual(len(self.app["summary"]), CHAR_LIMITS["summary"])
        self.assertLessEqual(len(self.app["authorName"]), CHAR_LIMITS["author_name"])

    def test_every_text_field_validates(self):
        # Exercised through Config so the guard in validate() is what rejects.
        Config().validate()
        config_module.load(str(REPO_JSON))

    def test_the_char_limit_guard_actually_rejects(self):
        for name, limit in CHAR_LIMITS.items():
            with self.subTest(field=name):
                bad = Config()
                setattr(bad.app, name, "x" * (limit + 1))
                with self.assertRaises(ConfigError) as caught:
                    bad.validate()
                self.assertIn(str(limit), str(caught.exception))

    # --- the facts about who made this ------------------------------------
    def test_author_names_the_developer_and_the_publisher(self):
        self.assertEqual(self.app["authorName"], "FROMTOKYO / published by BILIBILI HK LIMITED")

    def test_nobody_who_did_not_make_the_game_is_credited(self):
        # Craft Egg made BanG Dream! Girls Band Party!, a different game.
        # Crediting them here was a plain factual error.
        for field in ("description", "authorName", "name", "summary"):
            with self.subTest(field=field):
                self.assertNotIn("Craft Egg", self.app[field])
        for field in ("name", "description"):
            with self.subTest(field=field):
                self.assertNotIn("Craft Egg", self.repo[field])

    def test_trademark_holder_is_named_in_the_repo_description(self):
        self.assertIn("Bushiroad", self.repo["description"])
        self.assertIn("FROMTOKYO", self.repo["description"])

    # --- categories -------------------------------------------------------
    def test_categories_are_games_the_client_knows(self):
        self.assertEqual(self.app["categories"], ["Party Game"])
        for category in self.app["categories"]:
            self.assertIn(category, GAMES_GROUP)

    def test_categories_are_not_the_group_name(self):
        # "Games" is a group in the current client, not a category. Using it
        # would file the app under Misc instead of Games.
        self.assertNotIn("Games", self.app["categories"])

    # --- the repository presents itself correctly --------------------------
    def test_repo_name_says_it_is_unofficial(self):
        self.assertIn("Unofficial", self.repo["name"])
        self.assertIn("Unofficial", self.repo["description"])

    def test_app_name_is_still_the_games_real_name(self):
        # Only the *repository* is unofficial. The app is BILIBILI's build of
        # the official game, and renaming it here would misdescribe the APK.
        self.assertEqual(self.app["name"], "BanG Dream! Our Notes")
        self.assertNotIn("Unofficial", self.app["name"])

    def test_repo_description_does_not_claim_to_be_official(self):
        self.assertNotIn("Official Android builds", self.repo["description"])


class DefaultsMatchShippedConfigTests(unittest.TestCase):
    """The code defaults and ``repo.json`` must not drift apart.

    ``repo.json`` always wins, so a drift is invisible until someone deletes
    the file - and then they silently get a differently-named repository with a
    different author, with no error anywhere.
    """

    def setUp(self):
        self.shipped = config_module.load(str(REPO_JSON))
        self.defaults = Config()

    def test_repo_fields_match(self):
        for name in ("name", "description", "icon", "mirror_country_code", "max_age"):
            with self.subTest(field=name):
                self.assertEqual(
                    getattr(self.defaults.repo, name), getattr(self.shipped.repo, name)
                )

    def test_app_fields_match(self):
        for name in ("name", "summary", "author_name", "categories", "license", "website"):
            with self.subTest(field=name):
                self.assertEqual(
                    getattr(self.defaults.app, name), getattr(self.shipped.app, name)
                )

    def test_description_lives_in_one_place_only(self):
        # Long enough that a second copy would only let the two drift, and the
        # app description is optional, so an absent one is a valid state.
        self.assertEqual(self.defaults.app.description, "")
        self.assertNotEqual(self.shipped.app.description, "")


if __name__ == "__main__":
    unittest.main()
