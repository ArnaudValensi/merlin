"""voice/speech.py: what the voice reads, and how the answer is cut.

Nova (the founder's prototype) is the reference behaviour; the differences are
deliberate: the code phrase follows the voice language, and line breaks survive
``speakable`` so a list still ends its pieces at each item.
"""

from voice.speech import CODE_PHRASE, Chunker, speakable


class TestSpeakable:
    def test_plain_prose_is_unchanged(self):
        assert speakable("The build passed. Nothing else to do.") == (
            "The build passed. Nothing else to do."
        )

    def test_code_fence_becomes_the_phrase_in_the_voice_language(self):
        text = "Here it is:\n```python\nprint('hi')\n```\nRun it."
        assert speakable(text, "en") == f"Here it is:\n{CODE_PHRASE['en']}\nRun it."
        assert CODE_PHRASE["fr"] in speakable(text, "fr")
        assert "print" not in speakable(text, "fr")

    def test_unknown_language_falls_back_to_english(self):
        assert speakable("```\nx\n```", "de") == CODE_PHRASE["en"]

    def test_unclosed_fence_is_dropped_to_the_end(self):
        assert speakable("Look:\n```bash\nrm -rf build", "en") == (
            f"Look:\n{CODE_PHRASE['en']}"
        )

    def test_answer_that_is_only_code(self):
        assert speakable("```js\nlet a = 1\n```") == CODE_PHRASE["en"]

    def test_inline_code_keeps_its_content(self):
        assert speakable("Run `make test` now.") == "Run make test now."

    def test_links_keep_their_label_and_urls_go(self):
        text = "See [the docs](https://example.com/docs) or https://x.io/a?b=1 now."
        assert speakable(text) == "See the docs or now."

    def test_markdown_marks_and_list_markers_go(self):
        text = "# Title\n\n- **first** item\n* second\n1. third\n> quoted"
        assert speakable(text) == "Title\nfirst item\nsecond\nthird\nquoted"

    def test_table_rows_and_rules_go(self):
        text = "Before.\n| a | b |\n|---|---|\n| 1 | 2 |\n---\nAfter."
        assert speakable(text) == "Before.\nAfter."

    def test_inner_underscores_read_as_spaces(self):
        assert speakable("The agent_state option, _emphasis_.") == (
            "The agent state option, emphasis."
        )

    def test_emoji_go(self):
        assert speakable("Done ✅ 🎉 all good.") == "Done all good."

    def test_whitespace_is_collapsed(self):
        assert speakable("  a   b \n\n\n  c  ") == "a b\nc"

    def test_empty(self):
        assert speakable("") == ""
        assert speakable("**  **") == ""


class TestChunker:
    def test_first_piece_is_short_for_low_latency(self):
        c = Chunker()
        assert c.feed("Sure, on it now. Here is the rest of it. ") == [
            "Sure, on it now."
        ]
        assert c.flush() == ["Here is the rest of it."]

    def test_a_too_short_first_sentence_joins_the_next(self):
        c = Chunker()
        assert c.feed("Yes. I will do that now. ") == ["Yes. I will do that now."]

    def test_later_pieces_gather_short_sentences(self):
        c = Chunker()
        c.feed("First sentence here. ")
        assert c.feed("One. Two words here. ") == ["One. Two words here."]

    def test_waits_for_a_boundary(self):
        c = Chunker()
        assert c.feed("No boundary yet") == []
        assert c.feed(" but now there is. ") == ["No boundary yet but now there is."]

    def test_a_line_break_always_ends_a_piece(self):
        c = Chunker()
        c.feed("A first sentence is here. ")
        assert c.feed("item one\nitem two\n") == ["item one", "item two"]

    def test_flush_returns_the_rest(self):
        c = Chunker()
        c.feed("Trailing words without a stop")
        assert c.flush() == ["Trailing words without a stop"]
        assert c.flush() == []

    def test_short_piece_held_back_until_flush(self):
        c = Chunker()
        c.feed("A first sentence is here. ")
        assert c.feed("Ok. ") == []
        assert c.flush() == ["Ok."]

    def test_french_punctuation(self):
        c = Chunker()
        assert c.feed("D'accord, je regarde. « Fini. » Voilà ! ") == [
            "D'accord, je regarde.",
            "« Fini. » Voilà !",
        ]

    def test_decimal_numbers_do_not_split(self):
        c = Chunker()
        assert c.feed("Version 3.5 is out today. ") == ["Version 3.5 is out today."]

    def test_speakable_then_chunker(self):
        c = Chunker()
        text = speakable("All done here.\n```\ncode\n```\n- next step\n", "en")
        pieces = c.feed(text + "\n") + c.flush()
        assert pieces == ["All done here.", f"{CODE_PHRASE['en']} next step"]

    def test_pieces_carry_no_line_breaks(self):
        c = Chunker()
        assert c.feed("Done.\nThe code is ready. ") == ["Done. The code is ready."]
