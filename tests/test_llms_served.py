"""`llms.txt` and the guides are served without authentication."""

import re

import pytest

from app.api import llms

GUIDES = [
    "quickstart",
    "configuration",
    "setup",
    "messaging",
    "operators",
    "agent-behaviour",
    "api-conventions",
    "operations",
]


async def test_llms_txt_served_with_guides_in_order(client):
    response = await client.get("/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    links = re.findall(r"\]\((docs/guides/[^)]+)\)", response.text)
    assert links == [f"docs/guides/{n}.md" for n in GUIDES]
    assert "/openapi.json" in response.text
    assert "docs/ARCHITECTURE.md" in response.text


async def test_guide_in_patched_dir_is_served(client, tmp_path, monkeypatch):
    (tmp_path / "demo.md").write_text("# Demo\n")
    monkeypatch.setattr(llms, "GUIDES_DIR", tmp_path)
    response = await client.get("/docs/guides/demo.md")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.text == "# Demo\n"


@pytest.mark.parametrize(
    "name",
    [
        "missing",
        "Setup",
        "a" * 65,
        "..%2FREADME",
        "../README",
        "%2e%2e/README",
        "secret.txt",
        "a%00b",
        "demo.md",
    ],
)
async def test_bad_guide_names_are_404(client, tmp_path, monkeypatch, name):
    (tmp_path / "Setup.md").write_text("x")
    (tmp_path.parent / "README.md").write_text("outside")
    (tmp_path / "secret.txt.md").write_text("x")
    monkeypatch.setattr(llms, "GUIDES_DIR", tmp_path)
    response = await client.get(f"/docs/guides/{name}.md")
    assert response.status_code == 404


async def test_symlink_escape_is_404(client, tmp_path, monkeypatch):
    outside = tmp_path / "outside.md"
    outside.write_text("secret")
    guides = tmp_path / "guides"
    guides.mkdir()
    (guides / "link.md").symlink_to(outside)
    monkeypatch.setattr(llms, "GUIDES_DIR", guides)
    assert (await client.get("/docs/guides/link.md")).status_code == 404


async def test_architecture_served(client):
    response = await client.get("/docs/ARCHITECTURE.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")


async def test_swagger_and_openapi_still_work(client):
    assert (await client.get("/docs")).status_code == 200
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert "/llms.txt" not in response.json()["paths"]


def test_dockerfile_copies_docs_and_dockerignore_allows_them():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "docker" / "Dockerfile").read_text()
    assert "llms.txt /srv/llms.txt" in dockerfile
    assert "docs /srv/docs" in dockerfile
    assert "docs" not in (root / ".dockerignore").read_text().split()
