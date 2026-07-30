from bminfo.talkgroups import PINNED_TALKGROUPS, classify_talkgroup


def test_talkgroup_country_mapping_uses_mcc():
    assert classify_talkgroup(21435) == ("ES", "Europe", "Spain")
    assert classify_talkgroup(26201) == ("DE", "Europe", "Germany")


def test_pinned_sala_andalucia_metadata_is_canonical():
    assert PINNED_TALKGROUPS == ((214001, "Sala Andalucía", "ES", "Europe", "Spain"),)
    assert classify_talkgroup(214001) == ("ES", "Europe", "Spain")


def test_talkgroup_special_and_global_mapping():
    assert classify_talkgroup(9001) == ("Global", "Global", "Global")
    assert classify_talkgroup(25701) == ("BY", "Europe", "Belarus")
    assert classify_talkgroup(64701) == ("RE", "Africa", "Réunion")


def test_unknown_talkgroup_is_other():
    assert classify_talkgroup(123456) == ("XX", "Other", "Other")
