from termroom.run_sources import build_public_git_clone_invocation


def test_anonymous_git_clone_does_not_set_empty_tls_client_certificate_paths() -> None:
    invocation = build_public_git_clone_invocation(
        "https://github.com/octocat/Hello-World.git",
        git_path="/usr/bin/git",
        askpass_path="/tmp/git-askpass",
        empty_home="/tmp/git-home",
        destination="/tmp/work.tmp",
    )

    assert "http.sslCert=" not in invocation.argv
    assert "http.sslKey=" not in invocation.argv
    assert invocation.env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert invocation.env["GIT_CONFIG_GLOBAL"] == "/dev/null"
