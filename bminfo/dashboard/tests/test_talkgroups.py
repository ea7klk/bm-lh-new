from django.test import SimpleTestCase

from dashboard.talkgroups import classify_talkgroup


class TalkgroupClassificationTests(SimpleTestCase):
    def test_spanish_talkgroup_uses_mcc_mapping(self):
        self.assertEqual(classify_talkgroup(21403), ("ES", "Europe", "Spain"))

    def test_global_talkgroups_use_global_geography(self):
        self.assertEqual(classify_talkgroup(91), ("Global", "Global", "Global"))

    def test_north_american_talkgroup_uses_country_and_continent(self):
        self.assertEqual(classify_talkgroup(3100), ("US", "North America", "United States"))
