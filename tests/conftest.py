import re

import pytest
from fastapi.testclient import TestClient

from rufur.app import build_app
from rufur.config import load_config
from rufur.embed import HashEmbedder
from rufur.llm import FakeLLM
from rufur.service import MemoryService


def fake_handler(role: str, prompt: str):
    """Deterministic LLM stand-in covering extraction and conflict classification."""
    if role == "extract":
        claims = []
        for line in prompt.splitlines():
            m = re.match(r'<event index="(\d+)" role="(\w+)">(.*)</event>', line)
            if not m:
                continue
            idx, content = int(m.group(1)), m.group(3)
            if city := re.search(r"I live in (\w+)", content):
                claims.append(dict(event=idx, type="fact", subject="user", predicate="home city", value=city.group(1),
                                   text=f"The user lives in {city.group(1)}.", quote=city.group(0)))
            if city := re.search(r"moved to (\w+)", content):
                claims.append(dict(event=idx, type="fact", subject="user", predicate="home city", value=city.group(1),
                                   text=f"The user moved to {city.group(1)}.", quote=city.group(0)))
            if "HALLUCINATE" in content:
                claims.append(dict(event=idx, type="fact", subject="user", predicate="pet", value="dragon",
                                   text="The user owns a dragon.", quote="I have a pet dragon"))
            if ed := re.search(r"I prefer (\w+) for editing", content):
                claims.append(dict(event=idx, type="preference", subject="user", predicate="preferred editor",
                                   value=ed.group(1), text=f"The user prefers {ed.group(1)} for editing.", quote=ed.group(0)))
        return {"claims": claims}
    if role == "classify" and prompt.startswith("A: "):  # same-attribute check: "A: subject / attribute / value"
        a, b = (line.split(" / ")[1] for line in prompt.splitlines()[:2])
        return {"same": a.strip().lower() == b.strip().lower()}
    if role == "classify":
        new = prompt.split("NEW:", 1)[1]
        if "moved" in new:
            return {"outcome": "update", "reason": "moved"}
        if "at work" in new:
            return {"outcome": "context", "scope_old": "personal", "scope_new": "work", "reason": "different context"}
        return {"outcome": "unclear", "reason": "conflict"}
    return None  # probe/answer roles: behave as unavailable


@pytest.fixture
def cfg(tmp_path):
    # Rerankers are real models; tests that need one pass a fake.
    return load_config(tmp_path, embed_provider="hash", embed_dim=256, fast_reranker="none", deep_reranker="none")


@pytest.fixture
def service(cfg):
    svc = MemoryService(cfg, llm=FakeLLM(cfg, fake_handler), embedder=HashEmbedder(cfg.embed_dim))
    yield svc
    svc.close()


@pytest.fixture
def client(cfg, service):
    with TestClient(build_app(cfg, service)) as c:
        yield c
