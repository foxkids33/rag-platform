from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.db.models import Document


def _migration():
    path = Path(__file__).parents[1] / "alembic/versions/0012_confluence_knowledge_base_sources.py"
    spec = spec_from_file_location("confluence_kb_sources_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = MagicMock()
    return module


def test_upgrade_preserves_scope_and_workspace_index():
    migration = _migration()
    migration.upgrade()
    args = migration.op.create_check_constraint.call_args.args
    assert "knowledge_base_version_id IS NOT NULL" in args[2]
    assert "external_id IS NOT NULL" in args[2]
    assert migration.op.create_index.call_args.args[0] == "uq_documents_kb_external_source"
    assert migration.op.create_index.call_args.kwargs["unique"] is True
    names = {constraint.name for constraint in Document.__table__.constraints}
    assert "ck_document_exactly_one_scope" in names
    assert "uq_documents_workspace_external_source" in {
        index.name for index in Document.__table__.indexes
    }


def test_downgrade_refuses_to_remove_existing_kb_sources():
    migration = _migration()
    migration.op.get_bind.return_value.execute.return_value.scalar.return_value = True
    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        migration.downgrade()
    migration.op.drop_index.assert_not_called()
    migration.op.drop_constraint.assert_not_called()


def test_downgrade_restores_constraint_when_no_kb_sources():
    migration = _migration()
    migration.op.get_bind.return_value.execute.return_value.scalar.return_value = False
    migration.downgrade()
    assert migration.op.drop_index.call_args.args[0] == "uq_documents_kb_external_source"
    expression = migration.op.create_check_constraint.call_args.args[2]
    assert "workspace_id IS NOT NULL AND external_id IS NOT NULL" in expression
