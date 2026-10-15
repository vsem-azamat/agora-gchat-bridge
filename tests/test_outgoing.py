from gchat_bridge.bridge import person_from
from gchat_bridge.outgoing import chat_markup, member_names, mention_markup


def test_markdown_to_chat():
    assert chat_markup("**bold** and *it* and ~~gone~~") == "*bold* and _it_ and ~gone~"
    assert chat_markup("# Title\ntext") == "*Title*\ntext"
    assert chat_markup("see [docs](https://example.com/a)") == "see <https://example.com/a|docs>"


def test_markdown_leaves_code_lists_and_identifiers_alone():
    assert chat_markup("`**x**` and\n```\n*y*\n```") == "`**x**` and\n```\n*y*\n```"
    assert chat_markup("* item\n* item") == "* item\n* item"
    assert chat_markup("__init__.py and a*b*c") == "__init__.py and a*b*c"


def test_mentions():
    members = {"Ada": "users/1", "Ada Lovelace": "users/1", "Bob": "users/2"}
    assert mention_markup("@ada lovelace, @Bob!", members) == "<users/1>, <users/2>!"
    assert mention_markup("mail ada@Bob.org, @Bobby", members) == "mail ada@Bob.org, @Bobby"
    assert mention_markup("`@Bob`", members) == "`@Bob`"
    assert mention_markup("@Bob", {}) == "@Bob"


def test_member_names_take_unique_first_names_only():
    names = member_names(
        {"users/1": "Ada Lovelace", "users/2": "Ada Byron", "users/3": "Grace Hopper"}
    )
    assert names == {
        "Ada Lovelace": "users/1",
        "Ada Byron": "users/2",
        "Grace Hopper": "users/3",
        "Grace": "users/3",
    }


def test_member_names_drop_shared_full_names():
    assert member_names({"users/1": "Sam", "users/2": "Sam"}) == {}


def test_person_photo():
    data = {
        "names": [{"displayName": "Ada"}],
        "photos": [{"url": "https://example.com/p/abc=s100", "metadata": {"primary": True}}],
    }
    person = person_from("users/1", data)
    assert person.name == "Ada"
    assert person.photo_url == "https://example.com/p/abc=s160"
    assert person.rev and person.rev != "none"
    same = person_from("users/1", {**data, "photos": [{"url": "https://example.com/p/abc=s64-c"}]})
    assert same.rev == person.rev
    default = person_from("users/1", {"photos": [{"url": "https://x/y=s100", "default": True}]})
    assert (default.name, default.rev) == ("user 1", "none")
    assert person_from("users/1", None).rev == ""


def test_mentions_leave_urls_and_links_alone():
    members = {"Ada": "users/1"}
    assert mention_markup("https://medium.com/@Ada/post", members) == "https://medium.com/@Ada/post"
    assert (
        mention_markup("<https://x.org/a|@Ada> @Ada", members) == "<https://x.org/a|@Ada> <users/1>"
    )
    text = mention_markup(chat_markup("[@Ada](https://x.org/a)"), members)
    assert text == "<https://x.org/a|@Ada>"


def test_mention_with_unusual_case_folding_does_not_crash():
    assert mention_markup("hi @İris", {"Iris": "users/1"}) in ("hi <users/1>", "hi @İris")


def test_links_with_parentheses_and_unsafe_characters():
    assert chat_markup("[Foo](https://en.wikipedia.org/wiki/Foo_(bar))") == (
        "<https://en.wikipedia.org/wiki/Foo_(bar)|Foo>"
    )
    assert chat_markup("[a](https://x.org/a|b)") == "[a](https://x.org/a|b)"
    assert chat_markup("[a > b](https://x.org/)") == "[a > b](https://x.org/)"


def test_person_without_photos_is_unknown_not_none():
    person = person_from("users/1", {"names": [{"displayName": "Ada"}]})
    assert person.rev == "" and person.named
    assert not person_from("users/1", None).named
