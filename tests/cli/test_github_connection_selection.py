from __future__ import annotations

from click.testing import CliRunner

from infrastructure.harness_providers.integration_selection import (
    current_github_connection_id,
)
from surfaces.cli.app import cli


def test_github_connection_option_accepts_the_id_emitted_by_integrations_list(
    monkeypatch,
) -> None:
    selected: list[str | None] = []
    monkeypatch.setattr(
        "surfaces.cli.app.render_landing",
        lambda _group: selected.append(current_github_connection_id()),
    )

    result = CliRunner().invoke(
        cli,
        ["--github-connection-id", "github-deadbeef", "--no-interactive"],
    )

    assert result.exit_code == 0
    assert selected == ["github-deadbeef"]
    assert current_github_connection_id() is None
