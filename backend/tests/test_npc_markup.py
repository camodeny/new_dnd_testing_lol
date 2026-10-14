"""NPC dialogue markup in DM narration text."""
from app.dm.npc_markup import NpcLine, iter_npc_lines, render_npc_line, strip_npc_markup


def test_render_round_trips_through_parse():
    text = render_npc_line('Bram "Two-Fingers" Holt', "Pay up.")
    assert text == '<npc speaker="Bram &quot;Two-Fingers&quot; Holt">"Pay up."</npc>'
    assert list(iter_npc_lines(text)) == [NpcLine(speaker='Bram "Two-Fingers" Holt', text='"Pay up."')]


def test_parse_finds_every_line_and_tolerates_missing_speaker():
    text = (
        'The rider leans forward.\n\n<npc speaker="Rider">"The debt is due."</npc>\n\n'
        "A pause.\n\n<npc>“Before sunrise.”</npc>"
    )
    assert list(iter_npc_lines(text)) == [
        NpcLine(speaker="Rider", text='"The debt is due."'),
        NpcLine(speaker="", text="“Before sunrise.”"),
    ]


def test_strip_flattens_lines_and_drops_unclosed_tags():
    text = 'The rider leans forward. <npc speaker="Rider">"The debt is due."</npc> <npc speaker="Rider">"And'
    assert strip_npc_markup(text) == 'The rider leans forward. Rider: "The debt is due." "And'
