from termroom.app import _localized_remote_run_error_detail


def test_outside_symlink_failure_explains_how_to_recover() -> None:
    run = {
        "error_code": "source_symlink_outside",
        "error_detail": "Workspace snapshot symlink points outside the selected folder",
    }

    korean = _localized_remote_run_error_detail("ko", run)
    english = _localized_remote_run_error_detail("en", run)

    assert korean is not None
    assert "symlink" in korean
    assert "하위 폴더" in korean
    assert english is not None
    assert "points outside" in english
    assert "subfolder" in english


def test_special_file_failure_explains_how_to_recover() -> None:
    run = {
        "error_code": "source_special_file",
        "error_detail": "Workspace snapshot contains an unsupported special file",
    }

    korean = _localized_remote_run_error_detail("ko", run)
    english = _localized_remote_run_error_detail("en", run)

    assert korean is not None
    assert "special file" in korean
    assert "제외" in korean
    assert english is not None
    assert "special file" in english
    assert "Exclude" in english
