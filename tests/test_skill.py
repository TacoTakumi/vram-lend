"""The shipped skill: every required item present, public style kept."""

import re
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "src" / "vram_lend" / "skills" / "vram-lend" / "SKILL.md"
TEXT = SKILL.read_text(encoding="ascii")


def test_front_matter_names_the_skill_and_its_triggers():
    head, body = TEXT.split("---\n", 2)[1:]
    assert "name: vram-lend" in head
    assert "description:" in head
    for trigger in ("CUDA out of memory", "too little free memory", "-gpu host",
                    "while a model is served"):
        assert trigger in head, trigger
    assert "Do NOT use" in head and "ComfyUI" in head


@pytest.mark.parametrize("item", [
    "nvidia-smi --query-gpu=index,pci.bus_id",
    "vram-lend state",
    "vram-lend run --gpu",
    "vram-lend lend --gpu",
    "vram-lend return --id",
    "VRAM_LEND_SERVER",
    "VRAM_LEND_API_KEY",
    "VRAM_LEND_LEASE",
    "Record the id",
])
def test_steps_are_present(item):
    assert item in TEXT, item


@pytest.mark.parametrize("status", ["409", "429", "503", "501", "404", "401", "409 retry"])
def test_every_refusal_has_a_row(status):
    rows = [line for line in TEXT.splitlines() if line.startswith("| `")]
    assert any(f"({status}" in row or f"({status})" in row or status in row for row in rows), status


@pytest.mark.parametrize("rule", [
    "Never start, load, swap, stop or restart a model",
    "Never search the process list",
    "ask the user for the engine or llama-swap address",
    "Never use memory after a 503",
    "When nothing lends, stop and report",
    "ComfyUI under llama-swap is not a case for this skill",
])
def test_rules_are_present(rule):
    assert rule in TEXT, rule


def test_manual_fallback_has_one_block_per_shape_with_three_calls():
    blocks = re.findall(r"```\n(.*?)```", TEXT, re.S)
    curl_blocks = [b for b in blocks if b.startswith("curl")]
    assert len(curl_blocks) == 2
    for block in curl_blocks:
        lines = [l for l in block.strip().splitlines() if l]
        assert len(lines) == 3
        assert "/vram/state" in lines[0] and "/vram/lend" in lines[1] and "/vram/return" in lines[2]
    assert "/upstream/MODEL/vram/state" in curl_blocks[1]


def test_public_style():
    assert all(ord(c) < 128 for c in TEXT)
    assert " -- " not in TEXT
    for home in re.findall(r"/home/([A-Za-z0-9_-]+)", TEXT):
        assert home == "you", home
    assert not re.search(r"\brig\b", TEXT)


def test_lend_step_records_the_endpoint_and_returns_with_it():
    assert "Record the id and the endpoint" in TEXT
    assert "vram-lend return --id 7 --endpoint" in TEXT
