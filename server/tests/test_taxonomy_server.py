"""Custom-push taxonomy additions."""

from server.bulletins.taxonomy import (
    DEFAULT_TAGS_FOR_NEW_USER,
    ORG_LABELS,
    TAG_LABELS,
    CanonicalOrg,
    ContentTag,
)


def test_server_org_present_with_label():
    assert CanonicalOrg.server.value == "server"
    assert ORG_LABELS[CanonicalOrg.server] == "伺服器"


def test_server_notification_tag_present_with_label():
    assert ContentTag.server_notification.value == "server_notification"
    assert TAG_LABELS[ContentTag.server_notification] == "伺服器通知"


def test_server_notification_not_in_default_tags():
    assert ContentTag.server_notification not in DEFAULT_TAGS_FOR_NEW_USER
