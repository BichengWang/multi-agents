from agent.bench.redact import REDACTED, Redactor


def test_known_values_and_key_shaped_strings_are_redacted_everywhere_in_nested_data():
    r = Redactor(["super-secret-value-1"], host_env=False)
    data = {
        "log": "x super-secret-value-1 y",
        "runs": [{"msg": "token sk-ant-api03-" + "Z9" * 20}, ("ghp_" + "a" * 36, 3)],
        "super-secret-value-1": None,
        "n": 5,
    }
    out = r.obj(data)
    assert out["log"] == f"x {REDACTED} y"
    assert out["runs"][0]["msg"] == f"token {REDACTED}"
    assert out["runs"][1] == [REDACTED, 3]
    assert REDACTED in out and out["n"] == 5
    assert "super-secret" not in repr(out) and "sk-ant" not in repr(out)


def test_env_dump_registers_only_credential_looking_variables_and_skips_known_plain_values():
    r = Redactor(host_env=False)
    added = r.add_env_dump(
        "PATH=/usr/bin:/bin\nOPENROUTER_API_KEY=or-key-value-123\nIS_SANDBOX=1\n"
        "SHORT_TOKEN=abc\nSOME_SECRET=plain-shared-value\nHOME=/root",
        ignore=["plain-shared-value"],
    )
    assert added == 1
    assert r.text("or-key-value-123 /usr/bin plain-shared-value") == f"{REDACTED} /usr/bin plain-shared-value"


def test_longer_secrets_win_over_their_prefixes_and_host_environment_is_read(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-anthropic-key-123456")
    r = Redactor(["host-anthropic-key-123456-extended-suffix"])
    assert r.text("a host-anthropic-key-123456-extended-suffix b host-anthropic-key-123456") == (
        f"a {REDACTED} b {REDACTED}"
    )
    assert r.text("nothing to see") == "nothing to see"
