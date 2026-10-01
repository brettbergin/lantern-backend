from lantern.errors import (
    BudgetExceededError,
    LanternError,
    SbxError,
    SbxNotFoundError,
    WorkerTimeoutError,
)


def test_hierarchy() -> None:
    assert issubclass(SbxNotFoundError, SbxError)
    assert issubclass(SbxError, LanternError)
    assert issubclass(WorkerTimeoutError, LanternError)
    assert issubclass(BudgetExceededError, LanternError)


def test_sbx_error_str_includes_context() -> None:
    err = SbxError(
        "sbx exec failed",
        argv=["sbx", "exec", "box", "true"],
        returncode=125,
        stderr="sandbox not running\n",
    )
    text = str(err)
    assert "sbx exec failed" in text
    assert "argv=sbx exec box true" in text
    assert "rc=125" in text
    assert "stderr=sandbox not running" in text


def test_sbx_error_str_minimal() -> None:
    assert str(SbxError("boom")) == "boom"
