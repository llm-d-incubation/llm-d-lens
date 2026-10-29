"""SQL-only cleanup never invokes the document migration or index builders."""

from unittest.mock import Mock

import pytest

from llm_d_lens.benchmark_rag import cutover
from llm_d_lens.benchmark_rag.publication_contracts import PublicationError
from llm_d_lens.db.dao.rag_transfer import reset_legacy_rag_storage


def test_quiescence_confirmation_precedes_files_or_database_access(tmp_path):
    engine = Mock()
    with pytest.raises(PublicationError, match="quiesced"):
        reset_legacy_rag_storage(tmp_path / "backups", engine=engine)
    engine.begin.assert_not_called()
    assert not (tmp_path / "backups").exists()


def test_cleanup_cli_requires_both_explicit_confirmations(tmp_path):
    for flags in ([], ["--execute"], ["--writers-quiesced"]):
        with pytest.raises(SystemExit):
            cutover.main([*flags, "--backup-dir", str(tmp_path)])


def test_cleanup_cli_delegates_only_sql_reset(tmp_path, monkeypatch, capsys):
    operation = Mock(return_value={"status": "committed", "indexes_rebuilt": False})
    monkeypatch.setattr(cutover, "reset_legacy_rag_storage", operation)
    assert cutover.main(["--execute", "--writers-quiesced", "--backup-dir", str(tmp_path)]) == 0
    operation.assert_called_once_with(tmp_path, writers_quiesced=True)
    assert '"indexes_rebuilt": false' in capsys.readouterr().out
