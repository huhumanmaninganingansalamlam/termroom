from termroom.terminal_control import TerminalControl


def test_only_last_input_client_owns_resize() -> None:
    control = TerminalControl()
    first = control.register("term")
    second = control.register("term")

    assert control.client_count("term") == 2
    assert not control.can_resize("term", first)
    assert not control.can_resize("term", second)

    control.mark_input("term", second)
    assert not control.can_resize("term", first)
    assert control.can_resize("term", second)

    control.unregister("term", second)
    assert control.client_count("term") == 1
    assert not control.can_resize("term", first)

    control.mark_input("term", first)
    assert control.can_resize("term", first)

    control.unregister("term", first)
    assert control.client_count("term") == 0


def test_new_connections_stay_passive_until_someone_types() -> None:
    control = TerminalControl()
    first = control.register("term")
    second = control.register("term")

    assert not control.can_resize("term", first)
    assert not control.can_resize("term", second)

    control.mark_input("term", first)
    third = control.register("term")

    assert control.can_resize("term", first)
    assert not control.can_resize("term", third)


def test_fresh_grid_bootstraps_only_its_first_browser_client() -> None:
    control = TerminalControl()
    control.mark_grid_fresh("term")
    first = control.register("term", device_id="device-a")
    second = control.register("term", device_id="device-b")

    assert control.can_resize("term", first)
    assert not control.can_resize("term", second)
    assert control.presence("term")["input_revision"] == 0
    assert control.resize_plan("term", first, rows=33, cols=162) == (True, True)

    control.unregister("term", first)
    assert not control.can_resize("term", second)


def test_fresh_grid_retries_when_the_bootstrap_client_disconnects_before_resize() -> None:
    control = TerminalControl()
    control.mark_grid_fresh("term")
    first = control.register("term")
    assert control.can_resize("term", first)

    control.unregister("term", first)
    second = control.register("term")

    assert control.can_resize("term", second)
    control.mark_input("term", second)
    assert control.can_resize("term", second)
    assert control.presence("term")["input_revision"] == 1


def test_presence_reports_client_count_and_input_device() -> None:
    control = TerminalControl()
    first = control.register("term")
    control.register("term")

    control.mark_input("term", first, "device-a")
    presence = control.presence("term")

    assert presence == {
        "count": 2,
        "input_revision": 1,
        "last_input_device_id": "device-a",
    }


def test_presence_deduplicates_clients_from_same_device() -> None:
    control = TerminalControl()
    first = control.register("term", device_id="device-a")
    second = control.register("term", device_id="device-a")
    control.register("term", device_id="device-b")

    control.mark_input("term", first, "device-a")
    assert control.client_count("term") == 3
    assert control.presence("term") == {
        "count": 2,
        "input_revision": 1,
        "last_input_device_id": "device-a",
    }

    control.unregister("term", first)
    assert control.client_count("term") == 2
    assert control.presence("term")["count"] == 2

    control.unregister("term", second)
    assert control.client_count("term") == 1
    assert control.presence("term")["count"] == 1


def test_input_from_unknown_client_does_not_take_resize_ownership() -> None:
    control = TerminalControl()
    first = control.register("term")

    control.mark_input("term", "unknown")

    assert not control.can_resize("term", first)


def test_resize_is_applied_only_when_owner_or_dimensions_change() -> None:
    control = TerminalControl()
    first = control.register("term")
    second = control.register("term")

    assert not control.should_resize("term", second, rows=24, cols=80)
    control.mark_input("term", second)
    assert control.should_resize("term", second, rows=24, cols=80)
    assert not control.should_resize("term", second, rows=24, cols=80)
    assert control.should_resize("term", second, rows=25, cols=80)

    control.mark_input("term", first)
    assert control.should_resize("term", first, rows=25, cols=80)
    assert not control.should_resize("term", second, rows=30, cols=100)
    assert not control.should_resize("term", first, rows=25, cols=80)
