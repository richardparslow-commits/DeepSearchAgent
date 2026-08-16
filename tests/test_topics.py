"""Guard against drift between the shared topic table and its derived views.

``topics.TOPICS`` is the single source of truth for topic names and detection
phrases. ``interpretation.ELEMENT_LIBRARY`` and ``impact.ISSUE_TAG_PATTERNS``
are built from it; these tests fail loudly if the three fall out of sync.
"""

from va_legal_agent.impact import ISSUE_TAG_PATTERNS
from va_legal_agent.interpretation import ELEMENT_LIBRARY
from va_legal_agent.topics import ISSUE_TAG_ORDER, TOPICS, TOPICS_BY_NAME, authority_weight_for


def test_authority_weight_defaults_to_zero_for_unknown_court():
    # Only listed courts carry an authority weight; anything else is unmapped
    # and must weigh nothing rather than crashing or defaulting to a nonzero.
    assert authority_weight_for("Some Unfamiliar Court") == 0
    assert authority_weight_for("") == 0


def test_element_library_mirrors_topics():
    # Same topics, same order, same detection phrases, and every element
    # carries non-empty interpretation guidance.
    assert [spec.name for spec in ELEMENT_LIBRARY] == [topic.name for topic in TOPICS]
    for spec, topic in zip(ELEMENT_LIBRARY, TOPICS):
        assert spec.phrases == topic.phrases
        assert spec.description and spec.guidance and spec.step


def test_issue_tag_patterns_derive_from_topics():
    # Each (phrase, tag) pair must be (primary phrase, topic name) from TOPICS,
    # in ISSUE_TAG_ORDER order.
    assert ISSUE_TAG_PATTERNS == tuple(
        (TOPICS_BY_NAME[name].phrases[0], name) for name in ISSUE_TAG_ORDER
    )


def test_topics_have_unique_names_and_primary_phrases():
    names = [topic.name for topic in TOPICS]
    assert len(names) == len(set(names))
    assert set(names) == set(TOPICS_BY_NAME)
    for topic in TOPICS:
        assert topic.name
        assert topic.phrases  # ISSUE_TAG_PATTERNS relies on phrases[0] existing
