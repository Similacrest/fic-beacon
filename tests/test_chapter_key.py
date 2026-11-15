from app.ongoing.story_key import chapter_key

RR = "https://www.royalroad.com/fiction/118891/{slug}/chapter/4328084/218-victors"


def test_royalroad_chapter_identity_ignores_the_slugs():
    old = RR.format(slug="b2-stubbing-nov-10th-new-life-as-a-max-level-archmage")
    new = RR.format(slug="new-life-as-a-max-level-archmage")
    assert chapter_key(old) == chapter_key(new) != chapter_key(RR.format(slug="x").replace("4328084", "1"))


def test_other_sites_use_their_chapter_ids():
    assert chapter_key("https://archiveofourown.org/works/9/chapters/55") == \
        chapter_key("http://www.archiveofourown.org/works/9/chapters/55?view_adult=true")
    assert chapter_key("https://www.fanfiction.net/s/123/4/Old-Title") == \
        chapter_key("https://m.fanfiction.net/s/123/4/New-Title")
    assert chapter_key("https://forums.sufficientvelocity.com/threads/x.1/post-99") == \
        chapter_key("https://forums.sufficientvelocity.com/threads/y.1/post-99#post-99")


def test_falls_back_to_the_normalised_url_and_none():
    assert chapter_key("https://Example.com/a/b/") == chapter_key("http://www.example.com/a/b")
    assert chapter_key("https://example.com/a") != chapter_key("https://example.com/b")
    assert chapter_key(None) is None and chapter_key("") is None
