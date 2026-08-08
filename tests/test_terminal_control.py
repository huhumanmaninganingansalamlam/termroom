from termroom.terminal_control import TerminalControl


def test_last_input_client_owns_resize_until_disconnect() -> None:
    control = TerminalControl()
    first = control.register("term")
    second = control.register("term")

    assert control.client_count("term") == 2
    assert not control.can_resize("term", first)
    assert control.can_resize("term", second)

    control.mark_input("term", second)
    assert not control.can_resize("term", first)
    assert control.can_resize("term", second)

    control.unregister("term", second)
    assert control.client_count("term") == 1
    assert control.can_resize("term", first)

    control.unregister("term", first)
    assert control.client_count("term") == 0


def test_newest_connection_controls_resize_until_someone_types() -> None:
    control = TerminalControl()
    first = control.register("term")
    second = control.register("term")

    assert control.can_resize("term", second)

    control.mark_input("term", first)
    third = control.register("term")

    assert control.can_resize("term", first)
    assert not control.can_resize("term", third)


def test_focused_view_can_claim_resize_before_any_input() -> None:
    control = TerminalControl()
    first = control.register("term")
    second = control.register("term")

    control.claim_view("term", first)
    assert control.can_resize("term", first)
    assert not control.can_resize("term", second)

    control.mark_input("term", second)
    control.claim_view("term", first)
    assert not control.can_resize("term", first)
    assert control.can_resize("term", second)


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


def test_input_from_unknown_client_does_not_take_resize_ownership() -> None:
    control = TerminalControl()
    first = control.register("term")

    control.mark_input("term", "unknown")

    assert control.can_resize("term", first)
