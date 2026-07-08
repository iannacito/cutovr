"""Test import_history.delete_* methods used by dev_reset_migration."""
import pytest
import tempfile
from pathlib import Path
from import_history import ImportHistory


@pytest.fixture
def history():
    """Create a temp import history DB for testing."""
    tmpdir = Path(tempfile.gettempdir()) / "test_import_history"
    tmpdir.mkdir(exist_ok=True)
    db_path = tmpdir / "test.db"
    db_path.unlink(missing_ok=True)
    return ImportHistory(db_path=str(db_path))


def test_delete_all_for_firm_realm_removes_imports(history):
    """Record imports, then delete by realm."""
    realm_id = "test-realm-1"

    # Record two imports for this realm
    imp1_id = history.record_import(
        job_id="job-1",
        realm_id=realm_id,
        file_sha256="file-sha-1",
        company_name="Test Co",
        transaction_count=10,
        debit_total="1000.00",
        credit_total="1000.00",
        status="success",
    )
    imp2_id = history.record_import(
        job_id="job-2",
        realm_id=realm_id,
        file_sha256="file-sha-2",
        company_name="Test Co",
        transaction_count=20,
        debit_total="2000.00",
        credit_total="2000.00",
        status="success",
    )

    # Import a third one for a different realm (should NOT be deleted)
    imp3_id = history.record_import(
        job_id="job-3",
        realm_id="other-realm",
        file_sha256="file-sha-3",
        company_name="Test Co",
        transaction_count=30,
        debit_total="3000.00",
        credit_total="3000.00",
        status="success",
    )

    # Delete all imports for the test realm
    deleted_count = history.delete_all_for_firm_realm(realm_id)
    assert deleted_count == 2

    # Verify the other realm's import still has history
    other_history = history.get_history_for_job("job-3")
    assert len(other_history) == 1


def test_delete_by_id_removes_single_import(history):
    """Delete a single import by ID."""
    realm_id = "test-realm"
    imp_id = history.record_import(
        job_id="job-1",
        realm_id=realm_id,
        file_sha256="file-sha",
        company_name="Test Co",
        transaction_count=10,
        debit_total="1000.00",
        credit_total="1000.00",
        status="success",
    )

    # Verify import exists
    hist_before = history.get_history_for_job("job-1")
    assert len(hist_before) == 1

    # Delete by ID
    result = history.delete_by_id(imp_id)
    assert result is True

    # Verify import is gone
    hist_after = history.get_history_for_job("job-1")
    assert len(hist_after) == 0


def test_delete_by_id_returns_false_for_nonexistent(history):
    """delete_by_id returns False when import not found."""
    result = history.delete_by_id(9999)
    assert result is False


def test_delete_import_record_removes_import(history):
    """Verify delete_import_record removes the import."""
    realm_id = "test-realm"
    imp_id = history.record_import(
        job_id="job-1",
        realm_id=realm_id,
        file_sha256="file-sha",
        company_name="Test Co",
        transaction_count=2,
        debit_total="1000.00",
        credit_total="1000.00",
        status="success",
    )

    # Verify it exists
    hist = history.get_history_for_job("job-1")
    assert len(hist) == 1

    # Delete the import
    history.delete_import_record(imp_id)

    # Verify import is gone
    hist = history.get_history_for_job("job-1")
    assert len(hist) == 0
