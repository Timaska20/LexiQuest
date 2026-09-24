from app.services.vot import build_synced_phrase_pairs


def test_moves_dangling_translation_fragment_to_next_source_cue():
    source = [
        {"start": 0.0, "end": 2.0, "text": "Well, he's at camp all week."},
        {"start": 2.0, "end": 4.5, "text": "I'm sorry you won't get to meet him."},
    ]
    target = [
        {"start": 0.0, "end": 2.4, "text": "Он в лагере на всю неделю. Жаль,"},
        {"start": 2.4, "end": 4.5, "text": "что ты не сможешь с ним познакомиться."},
    ]

    pairs = build_synced_phrase_pairs(source, target)

    assert pairs[0]["translated_text"] == "Он в лагере на всю неделю."
    assert pairs[1]["translated_text"] == "Жаль, что ты не сможешь с ним познакомиться."


def test_source_timing_stays_canonical():
    source = [
        {"start": 10.0, "end": 11.2, "text": "Hello."},
        {"start": 11.2, "end": 13.0, "text": "How are you?"},
    ]
    target = [
        {"start": 10.1, "end": 12.9, "text": "Привет. Как дела?"},
    ]

    pairs = build_synced_phrase_pairs(source, target)

    assert [(p["start"], p["end"]) for p in pairs] == [(10.0, 11.2), (11.2, 13.0)]
    assert pairs[0]["translated_text"] == "Привет."
    assert pairs[1]["translated_text"] == "Как дела?"


def test_multiple_target_cues_can_join_one_source_cue():
    source = [
        {"start": 0.0, "end": 2.0, "text": "I'm going home."},
    ]
    target = [
        {"start": 0.0, "end": 0.7, "text": "Я"},
        {"start": 0.7, "end": 2.0, "text": "иду домой."},
    ]

    pairs = build_synced_phrase_pairs(source, target)

    assert pairs[0]["translated_text"] == "Я иду домой."
