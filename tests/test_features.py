"""The rule that decides which ``uses-feature`` entries reach the index.

This is a regression test for a bug found by real users. An earlier version of
this project published ``glEsVersion196608`` and every ``required="false"``
feature, and F-Droid then reported the app as incompatible with the devices it
was perfectly able to run on.

The rule is taken from the reference implementation,
``fdroidserver/update.py``:

    feature = str(item.attrib.get(xmlns + 'name', ''))
    if not feature:
        continue
    ...
    required = item.attrib.get(xmlns + 'required')
    if required is None or required == 'true':
        manifest['features'].append({'name': feature})

So: named features only, required ones only. A feature declared solely as
``android:glEsVersion`` has no name and is never reported, which is also why
``androguard.get_features()`` - what fdroidserver actually calls - omits it.

F-Droid treats every feature in the index as mandatory, so an over-broad list is
not cosmetic: it excludes devices.
"""

import unittest

from bdon_fdroid import axml
from tests.test_axml import _AxmlBuilder, _manifest_document

#: The exact shape of the real BanG Dream! Our Notes manifest, transcribed from
#: the published APK that users reported as being wrongly marked incompatible.
BAN_G_DREAM_FEATURES = [
    # A GLES requirement with no android:name, therefore no name to report.
    {"android:glEsVersion": 0x30000},
    # All of these are explicitly optional.
    {"android:name": "android.hardware.vulkan.version", "android:required": "false"},
    {"android:name": "android.hardware.touchscreen", "android:required": "false"},
    {
        "android:name": "android.hardware.touchscreen.multitouch",
        "android:required": "false",
    },
    {
        "android:name": "android.hardware.touchscreen.multitouch.distinct",
        "android:required": "false",
    },
]


class UsesFeatureRuleTests(unittest.TestCase):
    def test_the_real_manifest_yields_no_features(self):
        manifest = axml.parse_manifest(
            _manifest_document(raw_features=BAN_G_DREAM_FEATURES)
        )
        self.assertEqual(
            manifest.features,
            [],
            "every entry in the real manifest is optional or unnamed, so the "
            "index must carry no feature list at all",
        )

    def test_gl_es_version_is_never_reported_as_a_feature(self):
        # The bug: emitting "glEsVersion196608" made clients demand a GLES
        # level as a hard requirement. No real F-Droid repository has it.
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[
                    {"android:glEsVersion": 0x30000},
                    {"android:glEsVersion": 0x20000},
                ]
            )
        )
        self.assertEqual(manifest.features, [])

    def test_optional_features_are_dropped(self):
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[
                    {"android:name": "android.hardware.camera", "android:required": "false"}
                ]
            )
        )
        self.assertEqual(manifest.features, [])

    def test_a_required_feature_is_kept(self):
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[
                    {"android:name": "android.hardware.camera", "android:required": "true"},
                    {
                        "android:name": "android.hardware.touchscreen",
                        "android:required": "false",
                    },
                ]
            )
        )
        self.assertEqual(manifest.features, ["android.hardware.camera"])

    def test_a_feature_with_no_required_attribute_is_kept(self):
        # `required` defaults to true when absent, so these are requirements.
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[
                    {"android:name": "android.hardware.nfc"},
                    {"android:name": "android.hardware.fingerprint"},
                ]
            )
        )
        self.assertEqual(
            manifest.features,
            ["android.hardware.nfc", "android.hardware.fingerprint"],
        )

    def test_required_false_as_a_boolean_is_also_dropped(self):
        # AXML encodes android:required either as the string "false" or as a
        # typed boolean, so both spellings have to be recognised.
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[
                    {"android:name": "android.hardware.camera", "android:required": False}
                ]
            )
        )
        self.assertEqual(manifest.features, [])

    def test_required_true_as_a_boolean_is_kept(self):
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[
                    {"android:name": "android.hardware.camera", "android:required": True}
                ]
            )
        )
        self.assertEqual(manifest.features, ["android.hardware.camera"])

    def test_legacy_android_feature_prefix_is_stripped(self):
        # fdroidserver does this for backwards compatibility.
        manifest = axml.parse_manifest(
            _manifest_document(
                raw_features=[{"android:name": "android.feature.location"}]
            )
        )
        self.assertEqual(manifest.features, ["location"])

    def test_duplicate_features_are_collapsed(self):
        manifest = axml.parse_manifest(
            _manifest_document(
                features=["android.hardware.nfc", "android.hardware.nfc"]
            )
        )
        self.assertEqual(manifest.features, ["android.hardware.nfc"])

    def test_a_feature_with_neither_name_nor_anything_else_is_skipped(self):
        builder = _AxmlBuilder()
        builder.element("uses-feature", {})
        manifest = axml.parse_manifest(builder.build())
        self.assertEqual(manifest.features, [])


class FeatureFreeManifestTests(unittest.TestCase):
    def test_permissions_are_unaffected_by_the_feature_rule(self):
        # Only uses-feature is filtered; permissions are a different field and
        # must still be reported in full.
        manifest = axml.parse_manifest(
            _manifest_document(
                permissions=[
                    "android.permission.INTERNET",
                    "com.android.vending.BILLING",
                    "android.permission.VIBRATE",
                ],
                raw_features=BAN_G_DREAM_FEATURES,
            )
        )
        self.assertEqual(
            manifest.permissions,
            [
                "android.permission.INTERNET",
                "com.android.vending.BILLING",
                "android.permission.VIBRATE",
            ],
        )


if __name__ == "__main__":
    unittest.main()
